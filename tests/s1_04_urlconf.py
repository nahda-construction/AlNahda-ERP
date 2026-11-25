from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.urls import path
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.access_control.policies import (
    Action,
    is_internal_employee,
    project_for,
    projects_for,
    work_node_for,
)
from apps.projects.models import Project, WorkNode


def _deny_unless_employee(request):
    if not request.user.is_authenticated:
        return JsonResponse({"message": "Sign in required."}, status=401)
    if not is_internal_employee(request.user):
        return JsonResponse({"message": "Permission denied."}, status=403)
    return None


def _not_found():
    return JsonResponse({"message": "Resource unavailable."}, status=404)


@ensure_csrf_cookie
@require_GET
def project_list(request):
    denied = _deny_unless_employee(request)
    if denied is not None:
        return denied
    visible = projects_for(request.user)
    query = request.GET.get("q")
    if query:
        visible = visible.filter(Q(code__icontains=query) | Q(name__icontains=query))
    projects = list(visible.order_by("code").values_list("code", flat=True))
    return JsonResponse({"count": len(projects), "projects": projects})


@require_GET
def project_detail(request, project_id):
    denied = _deny_unless_employee(request)
    if denied is not None:
        return denied
    try:
        project = project_for(request.user, project_id)
    except Project.DoesNotExist:
        return _not_found()
    return JsonResponse({"project": project.code})


@require_GET
def node_detail(request, project_id, node_id):
    denied = _deny_unless_employee(request)
    if denied is not None:
        return denied
    try:
        node = work_node_for(request.user, project_id, node_id)
    except (Project.DoesNotExist, WorkNode.DoesNotExist):
        return _not_found()
    return JsonResponse({"node": node.code})


def _approval_effect(project):
    """Test hook representing a later business operation."""


@require_POST
def approve_probe(request, project_id):
    denied = _deny_unless_employee(request)
    if denied is not None:
        return denied
    try:
        project = project_for(request.user, project_id)
    except Project.DoesNotExist:
        return _not_found()
    if not projects_for(request.user, Action.APPROVE_TEST_TRANSACTION).filter(pk=project.pk).exists():
        return JsonResponse({"message": "Permission denied."}, status=403)
    _approval_effect(project)
    return HttpResponse(status=204)


urlpatterns = [
    path("s1-04-probe/projects/", project_list, name="s104-projects"),
    path("s1-04-probe/projects/<uuid:project_id>/", project_detail, name="s104-project"),
    path("s1-04-probe/projects/<uuid:project_id>/nodes/<uuid:node_id>/", node_detail, name="s104-node"),
    path("s1-04-probe/projects/<uuid:project_id>/approve/", approve_probe, name="s104-approve"),
]
