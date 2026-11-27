from functools import wraps
from importlib.resources import files
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, JsonResponse
from django.views.decorators.http import require_safe
from ninja.errors import HttpError
from ninja.security import SessionAuth

from apps.access_control.policies import has_internal_permission, is_internal_employee


MAX_JSON_BYTES = 512 * 1024
ERRORS = {
    401: ("authentication_required", "يلزم تسجيل الدخول."),
    403: ("forbidden", "هذا الإجراء غير مسموح."),
    404: ("not_found", "المورد غير متاح."),
    409: ("conflict", "يتعارض الطلب مع أثر سابق أو حالة المعاملة."),
    422: ("validation_error", "مدخلات الطلب غير صالحة."),
}
ASSET_TYPES = {
    "favicon.png": "image/png",
    "favicon.svg": "image/svg+xml",
    "swagger-ui.css": "text/css",
    "swagger-ui-bundle.js": "text/javascript",
    "swagger-ui-init.js": "text/javascript",
}


def error_response(status, *, field_errors=None, csrf=False):
    code, message = ERRORS[status]
    if csrf:
        code, message = "csrf_failed", "تعذّر التحقق من حماية الطلب."
    body = {"code": code, "message": message, "correlation_id": str(uuid4())}
    if field_errors:
        body["field_errors"] = field_errors
    response = JsonResponse(body, status=status)
    response.headers["Cache-Control"] = "private, no-store"
    return response


class EmployeeSessionAuth(SessionAuth):
    def __call__(self, request):
        if not request.user.is_authenticated:
            return None
        if not is_internal_employee(request.user):
            raise PermissionDenied
        user = super().__call__(request)
        if request.method == "POST":
            try:
                content_length = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                raise HttpError(422, "Invalid content length") from None
            if not 0 <= content_length <= MAX_JSON_BYTES or request.content_type != "application/json":
                raise HttpError(422, "Expected bounded JSON")
        return user


def docs_permission_required(view):
    @wraps(view)
    def protected(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error_response(401)
        if not has_internal_permission(request.user, "view_internal_api_docs"):
            return error_response(403)
        return view(request, *args, **kwargs)

    return require_safe(protected)


@docs_permission_required
def docs_asset(request, asset):
    if asset not in ASSET_TYPES:
        raise Http404
    resource = files("ninja").joinpath("static", "ninja", asset)
    return FileResponse(resource.open("rb"), content_type=ASSET_TYPES[asset])
