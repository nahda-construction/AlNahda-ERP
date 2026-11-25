from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from apps.access_control.models import ProjectAccess, ProjectAccessEvent
from apps.projects.models import Project


class ProjectAccessSchemaTests(TestCase):
    fixtures = ["s1_03_demo"]

    @classmethod
    def setUpTestData(cls):
        accounts = get_user_model().objects
        cls.employee = accounts.create_user(username="schema-employee")
        cls.grantor = accounts.create_user(username="schema-grantor")
        cls.revoker = accounts.create_user(username="schema-revoker")
        cls.project = Project.objects.get(code="DEMO-A")

    def setUp(self):
        self.granted_at = timezone.now()

    def insert_membership(self, **overrides):
        fields = {
            "user": self.employee,
            "project": self.project,
            "role": ProjectAccess.Role.OPERATOR,
            "granted_by": self.grantor,
            "granted_at": self.granted_at,
        }
        return ProjectAccess.objects.create(**(fields | overrides))

    def assert_constraint_rejected(self, name, **overrides):
        with self.assertRaises(IntegrityError) as capture, transaction.atomic():
            self.insert_membership(**overrides)
        self.assertEqual(capture.exception.__cause__.diag.constraint_name, name)

    def test_postgresql_has_expected_unique_check_and_foreign_keys(self):
        self.assertEqual(connection.vendor, "postgresql")
        with connection.cursor() as cursor:
            membership = connection.introspection.get_constraints(
                cursor, ProjectAccess._meta.db_table,
            )
            events = connection.introspection.get_constraints(
                cursor, ProjectAccessEvent._meta.db_table,
            )

        unique = membership["project_access_user_project_role_unique"]
        self.assertTrue(unique["unique"])
        self.assertEqual(unique["columns"], ["user_id", "project_id", "role"])
        self.assertTrue(membership["project_access_role_valid"]["check"])
        self.assertTrue(membership["project_access_revocation_valid"]["check"])
        self.assertTrue(events["project_access_event_type_valid"]["check"])

        def foreign_keys(constraints):
            return {
                tuple(info["columns"]): info["foreign_key"]
                for info in constraints.values()
                if info["foreign_key"]
            }

        user_table = get_user_model()._meta.db_table
        self.assertEqual(foreign_keys(membership), {
            ("user_id",): (user_table, "id"),
            ("granted_by_id",): (user_table, "id"),
            ("project_id",): (Project._meta.db_table, "id"),
        })
        self.assertEqual(foreign_keys(events), {
            ("actor_id",): (user_table, "id"),
            ("membership_id",): (ProjectAccess._meta.db_table, "id"),
        })

    def test_invalid_role_and_duplicate_role_are_rejected_but_another_role_is_allowed(self):
        self.assert_constraint_rejected("project_access_role_valid", role="INVALID")
        self.insert_membership()
        self.assert_constraint_rejected("project_access_user_project_role_unique")
        self.insert_membership(role=ProjectAccess.Role.VIEWER)
        self.assertEqual(ProjectAccess.objects.filter(user=self.employee, project=self.project).count(), 2)

    def test_postgresql_rejects_each_invalid_revocation_state(self):
        cases = {
            "active with revocation date": {"active": True, "revoked_at": self.granted_at},
            "revoked without date": {"active": False, "revoked_at": None},
            "revoked before grant": {
                "active": False, "revoked_at": self.granted_at - timedelta(seconds=1),
            },
        }
        for case, fields in cases.items():
            with self.subTest(case=case):
                self.assert_constraint_rejected("project_access_revocation_valid", **fields)
        self.assertFalse(ProjectAccess.objects.exists())

    def test_postgresql_accepts_active_and_revoked_membership(self):
        active = self.insert_membership()
        revoked = self.insert_membership(
            role=ProjectAccess.Role.VIEWER, active=False, revoked_at=self.granted_at,
        )
        active.refresh_from_db()
        revoked.refresh_from_db()
        self.assertTrue(active.active)
        self.assertIsNone(active.revoked_at)
        self.assertFalse(revoked.active)
        self.assertEqual(revoked.revoked_at, revoked.granted_at)

    def test_protects_every_membership_reference(self):
        project = Project.objects.create(
            code="PROTECTED-BY-ACCESS", name="Membership only", owner_party=self.project.owner_party,
        )
        membership = self.insert_membership(project=project)
        for reference, target in (
            ("user", self.employee),
            ("project", project),
            ("granted_by", self.grantor),
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(ProtectedError) as capture:
                    target.delete()
                self.assertEqual(set(capture.exception.protected_objects), {membership})

    def test_protects_the_event_membership_and_the_separate_revocation_actor(self):
        membership = self.insert_membership(active=False, revoked_at=self.granted_at)
        revoked = ProjectAccessEvent.objects.create(
            membership=membership,
            actor=self.revoker,
            occurred_at=self.granted_at,
            event_type=ProjectAccessEvent.EventType.REVOKED,
        )
        for reference, target in (("membership", membership), ("actor", self.revoker)):
            with self.subTest(reference=reference):
                with self.assertRaises(ProtectedError) as capture:
                    target.delete()
                self.assertEqual(set(capture.exception.protected_objects), {revoked})
