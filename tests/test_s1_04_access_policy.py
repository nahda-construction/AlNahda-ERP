from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.access_control.models import ProjectAccess, ProjectAccessEvent
from apps.access_control.policies import (
    EMPLOYEE_GROUP,
    Action,
    has_internal_permission,
    is_internal_employee,
    project_for,
    projects_for,
    work_node_for,
    work_nodes_for,
)
from apps.access_control.services import grant_project_access, revoke_project_access
from apps.projects.models import Project, WorkNode


def access_permission(codename):
    return Permission.objects.get(
        content_type__app_label="access_control", content_type__model="projectaccess",
        codename=codename,
    )


class ProjectAccessPolicyTests(TestCase):
    fixtures = ["s1_03_demo"]

    @classmethod
    def setUpTestData(cls):
        cls.project_a = Project.objects.get(code="DEMO-A")
        cls.project_b = Project.objects.get(code="DEMO-B")
        accounts = get_user_model().objects
        staff = Group.objects.create(name=EMPLOYEE_GROUP)
        cls.operator = accounts.create_user(username="policy-operator")
        cls.viewer = accounts.create_user(username="policy-viewer")
        cls.manager = accounts.create_user(username="policy-manager")
        cls.grantor = accounts.create_user(username="policy-grantor")
        cls.revoker = accounts.create_user(username="policy-revoker")
        cls.docs_only = accounts.create_user(username="policy-docs")
        cls.outsider = accounts.create_user(username="policy-outside", is_staff=True, is_superuser=True)
        cls.superuser = accounts.create_user(username="policy-superuser", is_staff=True, is_superuser=True)
        staff.user_set.add(
            cls.operator, cls.viewer, cls.manager, cls.grantor, cls.revoker, cls.docs_only, cls.superuser,
        )
        cls.grantor.user_permissions.add(access_permission("manage_project_access"))
        admin_group = Group.objects.create(name="TEST_MEMBERSHIP_ADMIN")
        admin_group.permissions.add(access_permission("manage_project_access"))
        cls.revoker.groups.add(admin_group)
        cls.docs_only.user_permissions.add(access_permission("view_internal_api_docs"))
        for actor, project, role in (
            (cls.operator, cls.project_a, ProjectAccess.Role.OPERATOR),
            (cls.viewer, cls.project_b, ProjectAccess.Role.VIEWER),
            (cls.manager, cls.project_a, ProjectAccess.Role.MANAGER),
            (cls.manager, cls.project_b, ProjectAccess.Role.OPERATOR),
        ):
            grant_project_access(actor=cls.grantor, user=actor, project=project, role=role)

    def test_projects_and_nested_nodes_use_memberships(self):
        self.assertQuerySetEqual(projects_for(self.operator), [self.project_a])
        self.assertQuerySetEqual(projects_for(self.viewer), [self.project_b])
        node = WorkNode.objects.get(code="A-ITEM")
        self.assertEqual(work_node_for(self.operator, self.project_a.pk, node.pk), node)
        self.assertTrue(work_nodes_for(self.operator, self.project_a.pk).filter(pk=node.pk).exists())
        with self.assertRaises(Project.DoesNotExist):
            project_for(self.operator, self.project_b.pk)
        with self.assertRaises(WorkNode.DoesNotExist):
            work_node_for(self.operator, self.project_a.pk, WorkNode.objects.get(code="B-ITEM").pk)

    def test_actions_use_roles_only_within_the_same_project(self):
        self.assertQuerySetEqual(projects_for(self.manager, Action.APPROVE_TEST_TRANSACTION), [self.project_a])
        self.assertQuerySetEqual(projects_for(self.manager, Action.CANCEL_TEST_DRAFT), [self.project_a])
        self.assertQuerySetEqual(projects_for(self.manager, Action.APPROVE_TEST_STOCK_ISSUE), [self.project_a])
        self.assertQuerySetEqual(projects_for(self.manager, Action.CREATE_TEST_DRAFT), [self.project_b])
        self.assertFalse(projects_for(self.operator, Action.APPROVE_TEST_TRANSACTION).exists())
        self.assertFalse(projects_for(self.manager, "unknown-action").exists())

        grant_project_access(
            actor=self.grantor, user=self.manager, project=self.project_a, role=ProjectAccess.Role.OPERATOR,
        )
        self.assertQuerySetEqual(
            projects_for(self.manager, Action.CREATE_TEST_DRAFT), [self.project_a, self.project_b], ordered=False,
        )

    def test_viewer_requires_explicit_attachment_and_print_permissions_within_scope(self):
        self.assertQuerySetEqual(projects_for(self.operator, Action.DOWNLOAD_TEST_ATTACHMENT), [self.project_a])
        self.assertQuerySetEqual(projects_for(self.operator, Action.PRINT_TEST_DOCUMENT), [self.project_a])
        self.assertFalse(projects_for(self.viewer, Action.DOWNLOAD_TEST_ATTACHMENT).exists())
        self.assertFalse(projects_for(self.viewer, Action.PRINT_TEST_DOCUMENT).exists())
        self.viewer.user_permissions.add(access_permission("download_test_attachment"))
        self.assertQuerySetEqual(projects_for(self.viewer, Action.DOWNLOAD_TEST_ATTACHMENT), [self.project_b])
        self.assertFalse(projects_for(self.viewer, Action.PRINT_TEST_DOCUMENT).exists())
        self.viewer.user_permissions.add(access_permission("print_test_document"))
        self.assertQuerySetEqual(projects_for(self.viewer, Action.PRINT_TEST_DOCUMENT), [self.project_b])
        self.assertFalse(projects_for(self.viewer, Action.CREATE_TEST_DRAFT).exists())

    def test_access_and_documentation_do_not_follow_superuser_or_an_unrelated_permission(self):
        docs = "view_internal_api_docs"
        self.assertTrue(has_internal_permission(self.docs_only, docs))
        self.assertFalse(projects_for(self.docs_only).exists())
        self.assertFalse(has_internal_permission(self.operator, docs))
        self.assertFalse(has_internal_permission(self.superuser, docs))
        self.assertFalse(projects_for(self.superuser).exists())
        self.assertFalse(projects_for(self.grantor).exists())
        self.assertFalse(is_internal_employee(AnonymousUser()))
        self.outsider.user_permissions.add(access_permission(docs))
        ProjectAccess.objects.create(
            user=self.outsider, project=self.project_a, role=ProjectAccess.Role.MANAGER,
            granted_by=self.grantor, granted_at=timezone.now(),
        )
        self.assertFalse(has_internal_permission(self.outsider, docs))
        self.assertFalse(projects_for(self.outsider).exists())
        get_user_model().objects.filter(pk=self.docs_only.pk).update(is_active=False)
        self.assertFalse(has_internal_permission(self.docs_only, docs))

    def test_grant_and_revoke_preserve_actors_and_do_not_duplicate_events(self):
        membership = ProjectAccess.objects.get(user=self.operator, project=self.project_a)
        first_grant = membership.granted_at
        revoke_project_access(actor=self.revoker, membership_id=membership.pk)
        revoke_project_access(actor=self.revoker, membership_id=membership.pk)
        membership.refresh_from_db()
        first_revoke = membership.revoked_at
        self.assertFalse(membership.active)
        self.assertGreaterEqual(first_revoke, first_grant)
        self.assertFalse(projects_for(self.operator).exists())

        for _ in range(2):
            regranted = grant_project_access(
                actor=self.revoker, user=self.operator, project=self.project_a, role=ProjectAccess.Role.OPERATOR,
            )
            self.assertEqual(regranted.pk, membership.pk)
        membership.refresh_from_db()
        self.assertTrue(membership.active)
        self.assertIsNone(membership.revoked_at)
        self.assertEqual(membership.granted_by, self.revoker)
        self.assertQuerySetEqual(projects_for(self.operator), [self.project_a])
        self.assertEqual(list(membership.events.order_by("pk").values_list(
            "event_type", "actor_id", "occurred_at",
        )), [
            ("GRANTED", self.grantor.pk, first_grant),
            ("REVOKED", self.revoker.pk, first_revoke),
            ("GRANTED", self.revoker.pk, membership.granted_at),
        ])

    def test_project_manager_has_no_implicit_membership_management(self):
        membership = ProjectAccess.objects.get(user=self.operator, project=self.project_a)
        with self.assertRaises(PermissionDenied):
            revoke_project_access(actor=self.manager, membership_id=membership.pk)
        with self.assertRaises(PermissionDenied):
            grant_project_access(
                actor=self.manager, user=self.viewer, project=self.project_a, role=ProjectAccess.Role.VIEWER,
            )
        with self.assertRaises(ValidationError):
            grant_project_access(
                actor=self.grantor, user=self.outsider, project=self.project_a, role=ProjectAccess.Role.VIEWER,
            )
        membership.refresh_from_db()
        self.assertTrue(membership.active)
        self.assertEqual(membership.events.count(), 1)

    @patch("apps.access_control.services.ProjectAccessEvent.objects.create", side_effect=IntegrityError)
    def test_event_failure_rolls_back_membership_writes(self, create_event):
        membership = ProjectAccess.objects.get(user=self.operator, project=self.project_a)
        with self.assertRaises(IntegrityError):
            revoke_project_access(actor=self.revoker, membership_id=membership.pk)
        membership.refresh_from_db()
        self.assertTrue(membership.active)
        self.assertIsNone(membership.revoked_at)
        self.assertEqual(membership.events.count(), 1)

        with self.assertRaises(IntegrityError):
            grant_project_access(
                actor=self.grantor, user=self.viewer, project=self.project_a, role=ProjectAccess.Role.VIEWER,
            )
        self.assertFalse(ProjectAccess.objects.filter(user=self.viewer, project=self.project_a).exists())
        self.assertEqual(create_event.call_count, 2)
