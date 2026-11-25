from enum import StrEnum

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db.models import Q

from apps.projects.models import Project, WorkNode

from .models import ProjectAccess


EMPLOYEE_GROUP = "ERP_EMPLOYEE"


class Action(StrEnum):
    VIEW_PROJECT = "view_project"
    VIEW_WORK_NODES = "view_work_nodes"
    VIEW_TEST_TRANSACTION = "view_test_transaction"
    CREATE_TEST_DRAFT = "create_test_draft"
    APPROVE_TEST_TRANSACTION = "approve_test_transaction"
    CANCEL_TEST_DRAFT = "cancel_test_draft"
    APPROVE_TEST_STOCK_ISSUE = "approve_test_stock_issue"
    DOWNLOAD_TEST_ATTACHMENT = "download_test_attachment"
    PRINT_TEST_DOCUMENT = "print_test_document"


Role = ProjectAccess.Role
READ_ROLES = tuple(Role.values)
ACTION_ROLES = {
    Action.VIEW_PROJECT: READ_ROLES,
    Action.VIEW_WORK_NODES: READ_ROLES,
    Action.VIEW_TEST_TRANSACTION: READ_ROLES,
    Action.CREATE_TEST_DRAFT: (Role.OPERATOR,),
    Action.APPROVE_TEST_TRANSACTION: (Role.MANAGER,),
    Action.CANCEL_TEST_DRAFT: (Role.MANAGER,),
    Action.APPROVE_TEST_STOCK_ISSUE: (Role.MANAGER,),
    Action.DOWNLOAD_TEST_ATTACHMENT: (Role.OPERATOR,),
    Action.PRINT_TEST_DOCUMENT: (Role.OPERATOR,),
}
VIEWER_PERMISSION_ACTIONS = (Action.DOWNLOAD_TEST_ATTACHMENT, Action.PRINT_TEST_DOCUMENT)


def _employee_account(actor):
    accounts = get_user_model()._default_manager
    if not actor.is_authenticated or actor.pk is None:
        return accounts.none()
    return accounts.filter(pk=actor.pk, is_active=True, groups__name=EMPLOYEE_GROUP)


def is_internal_employee(actor):
    return _employee_account(actor).exists()


def has_internal_permission(actor, codename):
    employees = _employee_account(actor)
    return Permission.objects.filter(
        Q(user__in=employees) | Q(group__user__in=employees),
        content_type__app_label="access_control",
        content_type__model="projectaccess",
        codename=codename,
    ).exists()


def projects_for(actor, action=Action.VIEW_PROJECT):
    roles = ACTION_ROLES.get(action)
    if not roles:
        return Project.objects.none()
    if action in VIEWER_PERMISSION_ACTIONS and has_internal_permission(actor, action):
        roles += (Role.VIEWER,)
    memberships = ProjectAccess.objects.filter(
        user__in=_employee_account(actor), active=True, role__in=roles,
    )
    return Project.objects.filter(pk__in=memberships.values("project_id"))


def project_for(actor, project_id, action=Action.VIEW_PROJECT):
    return projects_for(actor, action).get(pk=project_id)


def work_nodes_for(actor, project_id):
    project = project_for(actor, project_id, Action.VIEW_WORK_NODES)
    return WorkNode.objects.filter(project=project)


def work_node_for(actor, project_id, work_node_id):
    return work_nodes_for(actor, project_id).get(pk=work_node_id)
