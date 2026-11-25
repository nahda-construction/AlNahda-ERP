from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from apps.projects.models import Party, Project, WorkNode


class ProjectSchemaTests(TestCase):
    fixtures = ["s1_03_demo"]

    def assert_constraint_rejected(self, name, action):
        with self.assertRaises(IntegrityError) as error, transaction.atomic():
            action()
        self.assertEqual(error.exception.__cause__.diag.constraint_name, name)

    def test_fixture_contains_both_projects_and_optional_activity_levels(self):
        self.assertEqual(Project.objects.count(), 2)
        leaf = WorkNode.objects.select_related("parent__parent__parent").get(code="A-ACT-2")
        self.assertEqual(leaf.parent.node_type, WorkNode.NodeType.ACTIVITY_1)
        self.assertEqual(leaf.parent.parent.node_type, WorkNode.NodeType.ITEM)
        self.assertEqual(leaf.parent.parent.parent.node_type, WorkNode.NodeType.SCOPE)
        self.assertFalse(WorkNode.objects.get(code="B-ITEM").children.exists())

    def test_postgresql_contains_the_parent_target_fk_and_party_indexes(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname IN (%s, %s, %s)
                """,
                ("worknode_parent_target_unique", "worknode_parent_project_type_fk",
                 "party_id_identity_kind_unique"),
            )
            definitions = dict(cursor.fetchall())
            cursor.execute(
                """
                SELECT i.indisunique, pg_get_expr(i.indpred, i.indrelid), pg_get_indexdef(i.indexrelid)
                FROM pg_index i
                WHERE i.indexrelid = %s::regclass
                """,
                ("party_single_company",),
            )
            unique, predicate, index_definition = cursor.fetchone()

        self.assertEqual(definitions["worknode_parent_target_unique"], "UNIQUE (id, project_id, node_type)")
        self.assertIn(
            "FOREIGN KEY (parent_id, project_id, parent_type) REFERENCES projects_worknode(id, project_id, node_type)",
            definitions["worknode_parent_project_type_fk"],
        )
        self.assertEqual(definitions["party_id_identity_kind_unique"], "UNIQUE (id, identity_kind)")
        self.assertTrue(unique)
        self.assertIn("COMPANY", predicate)
        self.assertIn("(identity_kind)", index_definition)

    def test_postgresql_rejects_invalid_tree_writes(self):
        project = Project.objects.get(code="DEMO-A")
        item = WorkNode.objects.get(code="A-ITEM")
        foreign_scope = WorkNode.objects.get(code="B-SCOPE")

        def insert(code, node_type, **parent_fields):
            WorkNode.objects.create(project=project, code=code, name=code, node_type=node_type, **parent_fields)

        self.assert_constraint_rejected(
            "worknode_valid_parent_level",
            lambda: insert("NO-PARENT", "ACTIVITY_2"),
        )
        self.assert_constraint_rejected(
            "worknode_valid_parent_level",
            lambda: insert("NO-TYPE", "ITEM", parent=item),
        )
        self.assert_constraint_rejected(
            "worknode_valid_parent_level",
            lambda: insert("SKIP-LEVEL", "ACTIVITY_2", parent=item, parent_type="ITEM"),
        )
        self.assert_constraint_rejected(
            "worknode_parent_project_type_fk",
            lambda: insert("WRONG-TYPE", "ITEM", parent=item, parent_type="SCOPE"),
        )
        self.assert_constraint_rejected(
            "worknode_parent_project_type_fk",
            lambda: insert("CROSS-PROJECT", "ITEM", parent=foreign_scope, parent_type="SCOPE"),
        )
        self.assert_constraint_rejected(
            "worknode_project_code_unique",
            lambda: insert("A-SCOPE", "SCOPE"),
        )

    def test_second_company_rejected_and_external_parties_remain_distinct(self):
        self.assert_constraint_rejected(
            "party_single_company",
            lambda: Party.objects.create(code="OTHER-COMPANY", name="Other", identity_kind=Party.Kind.COMPANY),
        )
        self.assertEqual(Party.objects.filter(identity_kind=Party.Kind.EXTERNAL).count(), 2)

    def test_archiving_retains_protected_references(self):
        project = Project.objects.get(code="DEMO-A")
        with self.assertRaises(ProtectedError):
            project.owner_party.delete()
        with self.assertRaises(ProtectedError):
            WorkNode.objects.get(code="A-SCOPE").delete()

        project.status = Project.Status.ARCHIVED
        project.save(update_fields=("status",))
        self.assertEqual(WorkNode.objects.get(code="A-ITEM").project.status, Project.Status.ARCHIVED)
        self.assertEqual(project.ownercontract.project_id, project.pk)
