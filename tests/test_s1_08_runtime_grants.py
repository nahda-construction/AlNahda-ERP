from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import TestCase
from django.utils import timezone
from psycopg import sql

from apps.access_control.models import ProjectAccess, ProjectAccessEvent
from apps.inventory.models import (
    Material,
    StockIssue,
    StockIssueLine,
    StockLocationBalance,
    StockMovement,
    ValuationPool,
    Warehouse,
)
from apps.projects.models import Party, Project, WorkNode
from apps.test_transactions.models import AuditEvent


SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "postgres" / "s1_08_inventory_runtime_grants.sql"


class StockRuntimeGrantTests(TestCase):
    fixtures = ["s1_03_demo"]

    @classmethod
    def setUpTestData(cls):
        cls.role_name = f"s108_runtime_{uuid4().hex}"
        cls.role = sql.Identifier(cls.role_name)
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            cls.owner = cursor.fetchone()[0]
            cursor.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT").format(cls.role))
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(cls.role))
            # The ERP-02 audit grants already exist when the inventory delta is applied.
            cursor.execute(sql.SQL("""
                GRANT SELECT, INSERT ON public.access_control_projectaccessevent,
                    public.test_transactions_auditevent TO {}
            """).format(cls.role))
            cursor.execute(sql.SQL("""
                GRANT USAGE ON SEQUENCE public.access_control_projectaccessevent_id_seq,
                    public.test_transactions_auditevent_id_seq TO {}
            """).format(cls.role))

        cls.actor = get_user_model().objects.create(username="synthetic-stock-approver", is_staff=True)
        cls.project = Project.objects.get(code="DEMO-A")
        cls.node = WorkNode.objects.get(project=cls.project, code="A-ITEM")
        cls.membership = ProjectAccess.objects.create(
            user=cls.actor, project=cls.project, role=ProjectAccess.Role.MANAGER,
            granted_by=cls.actor, granted_at=timezone.now(),
        )
        material = Material.objects.create(
            code="RUNTIME-MATERIAL", name="Synthetic stock", base_unit="DEMO_UNIT",
            valuation_method="DEMO_METHOD",
        )
        warehouse = Warehouse.objects.create(code="RUNTIME-WAREHOUSE", name="Synthetic warehouse")
        pool = ValuationPool.objects.create(
            scope_type=ValuationPool.ScopeType.PROJECT_STOCK, project=cls.project,
            owner=cls.project.owner_party, material=material,
        )
        cls.balance = StockLocationBalance.objects.create(pool=pool, warehouse=warehouse)
        cls.issue = StockIssue.objects.create(
            project=cls.project, work_node=cls.node, document_no="SYNTHETIC-ISSUE",
            created_by=cls.actor,
        )
        StockIssueLine.objects.create(issue=cls.issue, balance=cls.balance, qty=Decimal("1"), line_no=1)

    def execute(self, statement, params=None):
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            if cursor.description is not None:
                return cursor.fetchall()

    def apply_policy(self, *, revoke=False):
        # psql provides the settings and transaction in the runbook. Django's test
        # transaction supplies them here so CI does not need a separate psql client.
        _, marker, policy = SCRIPT.read_text(encoding="utf-8").partition("-- Inventory grant policy.\n")
        self.assertTrue(marker)
        policy, commit, trailing = policy.rpartition("\nCOMMIT;\n")
        self.assertTrue(commit)
        self.assertFalse(trailing)
        with transaction.atomic():
            self.execute("SELECT set_config('erp.runtime_role', %s, true)", [self.role_name])
            self.execute("SELECT set_config('erp.revoke_inventory_runtime', %s, true)", [str(revoke)])
            self.execute(policy)

    @contextmanager
    def runtime(self):
        with transaction.atomic():
            self.execute(sql.SQL("SET LOCAL ROLE {}").format(self.role))
            self.assertEqual(self.execute("SELECT current_user, session_user"), [(self.role_name, self.owner)])
            yield
            self.execute("RESET ROLE")

    def assert_denied(self, statement):
        with self.assertRaises(DatabaseError) as failure, transaction.atomic():
            self.execute(statement)
        self.assertEqual(failure.exception.__cause__.sqlstate, "42501")

    def test_runtime_reads_references_and_can_append_but_not_rewrite_history(self):
        self.apply_policy()
        stamp = timezone.now()
        with self.runtime():
            self.assertTrue(Material.objects.exists())
            self.assertTrue(Warehouse.objects.exists())
            self.assertTrue(ValuationPool.objects.exists())
            self.assertTrue(StockIssueLine.objects.exists())
            party = Party.objects.values_list("id", "active").get(pk=self.project.owner_party_id)
            self.assertEqual(party, (self.project.owner_party_id, True))
            self.assert_denied("SELECT name FROM public.projects_party")

            # The same column-level UPDATE grants also permit the posting service's row locks.
            StockLocationBalance.objects.select_for_update().get(pk=self.balance.pk)
            StockIssue.objects.select_for_update().get(pk=self.issue.pk)
            movement = StockMovement.objects.create(
                balance=self.balance, event_kind=StockMovement.EventKind.OPENING, signed_qty=Decimal("10"),
            )
            StockLocationBalance.objects.filter(pk=self.balance.pk).update(quantity=Decimal("10"))
            StockIssue.objects.filter(pk=self.issue.pk).update(
                status=StockIssue.Status.APPROVED, approved_by=self.actor, approved_at=stamp,
            )
            stock_event = AuditEvent.objects.create(
                project=self.project, actor=self.actor, stock_issue=self.issue,
                event_type=AuditEvent.EventType.APPROVED, prior_state="DRAFT", next_state="APPROVED",
            )
            membership_event = ProjectAccessEvent.objects.create(
                membership=self.membership, actor=self.actor, occurred_at=stamp,
                event_type=ProjectAccessEvent.EventType.GRANTED,
            )
            for model, record, column in (
                (StockMovement, movement, "signed_qty"),
                (AuditEvent, stock_event, "reason"),
                (ProjectAccessEvent, membership_event, "event_type"),
            ):
                with self.subTest(table=model._meta.db_table):
                    self.assertTrue(model.objects.filter(pk=record.pk).exists())
                    table = sql.Identifier("public", model._meta.db_table)
                    field = sql.Identifier(column)
                    self.assert_denied(sql.SQL("UPDATE {} SET {} = {} WHERE false").format(table, field, field))
                    self.assert_denied(sql.SQL("DELETE FROM {} WHERE false").format(table))
                    self.assert_denied(sql.SQL("TRUNCATE {}").format(table))

            self.assert_denied("UPDATE public.inventory_stocklocationbalance SET stock_state = 'AVAILABLE' WHERE false")
            self.assert_denied("UPDATE public.inventory_stockissue SET document_no = 'change' WHERE false")
            self.assert_denied("INSERT INTO public.inventory_stockissue DEFAULT VALUES")
            self.assert_denied("INSERT INTO public.inventory_stockissueline DEFAULT VALUES")
            self.assert_denied("INSERT INTO public.inventory_material DEFAULT VALUES")

    def test_reapply_clears_stale_inventory_grants_and_rollback_preserves_erp02(self):
        self.execute(sql.SQL("GRANT UPDATE (document_no) ON public.inventory_stockissue TO {}").format(self.role))
        self.execute(sql.SQL("GRANT DELETE ON public.inventory_stockmovement TO {}").format(self.role))
        self.apply_policy()
        self.apply_policy()
        with self.runtime():
            self.assert_denied("UPDATE public.inventory_stockissue SET document_no = 'change' WHERE false")
            self.assert_denied("DELETE FROM public.inventory_stockmovement WHERE false")
        self.apply_policy(revoke=True)
        self.apply_policy(revoke=True)
        with self.runtime():
            self.assert_denied("SELECT quantity FROM public.inventory_stocklocationbalance")
            self.assert_denied("SELECT id, active FROM public.projects_party")
            self.assert_denied("SELECT id FROM public.inventory_stockissue")
            self.assertFalse(AuditEvent.objects.exists())
            event = ProjectAccessEvent.objects.create(
                membership=self.membership, actor=self.actor, occurred_at=timezone.now(),
                event_type=ProjectAccessEvent.EventType.GRANTED,
            )
            self.assertTrue(ProjectAccessEvent.objects.filter(pk=event.pk).exists())
        self.apply_policy()
        with self.runtime():
            self.assertEqual(self.execute("SELECT active FROM public.projects_party WHERE id = %s", [
                self.project.owner_party_id,
            ]), [(True,)])

    def test_rejects_privileges_via_public_or_role_membership(self):
        for table, field in (
            (StockMovement._meta.db_table, "signed_qty"),
            (AuditEvent._meta.db_table, "reason"),
            (ProjectAccessEvent._meta.db_table, "event_type"),
        ):
            relation = sql.Identifier("public", table)
            with self.subTest(table=table):
                self.execute(sql.SQL("GRANT UPDATE ({}) ON {} TO PUBLIC").format(sql.Identifier(field), relation))
                with self.assertRaisesMessage(DatabaseError, "Unexpected effective UPDATE privilege"):
                    self.apply_policy()
                self.execute(sql.SQL("REVOKE UPDATE ({}) ON {} FROM PUBLIC").format(sql.Identifier(field), relation))
                with self.runtime():
                    self.assert_denied("SELECT quantity FROM public.inventory_stocklocationbalance")

        elevated = sql.Identifier(f"s108_parent_{uuid4().hex}")
        self.execute(sql.SQL("CREATE ROLE {}").format(elevated))
        self.execute(sql.SQL("GRANT DELETE ON public.inventory_stockmovement TO {}").format(elevated))
        self.execute(sql.SQL("GRANT {} TO {} WITH INHERIT TRUE").format(elevated, self.role))
        self.assertEqual(self.execute("SELECT has_table_privilege(%s, %s, 'DELETE')", [
            self.role_name, "public.inventory_stockmovement",
        ]), [(True,)])
        with self.assertRaisesMessage(DatabaseError, "independent, restricted login"):
            self.apply_policy()
