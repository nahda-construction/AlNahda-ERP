from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.urls import include, path
from django.views.decorators.http import require_safe

from apps.internal_api.access import docs_asset
from apps.internal_api.api import api


@require_safe
def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("employees/", include("apps.test_transactions.urls")),
    path("api/v1/docs/assets/ninja/<str:asset>", docs_asset, name="internal-api-docs-asset"),
    path("api/v1/", api.urls),
]
