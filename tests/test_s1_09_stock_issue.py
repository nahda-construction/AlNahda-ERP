from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.access_control.models import ProjectAccess
from apps.access_control.services import grant_project_access
from apps.inventory.models import StockIssue, StockIssueLine, StockLocationBalance, StockMovement, ValuationPool
from apps.inventory.services import (
    InsufficientStock,
    approve_stock_issue,
    ensure_general_stock_at_location,
    post_opening_stock,
)
from apps.projects.models import Party, Project, WorkNode
from apps.test_transactions.models import AuditEvent

from .test_s1_05_http import TEST_HASHERS, make_employee, make_grantor
from .test_s1_08_stock_rollback import OPENING_QUANTITY, seed_stock


def seed_approval(target):
    seed_stock(target)
    target.grantor = make_grantor()
    target.manager = make_employee("s109-project-manager")
    target.second_manager = make_employee("s109-second-project-manager")
    target.other_manager = make_employee("s109-other-project-manager")
    target.outsider = get_user_model().objects.create_user(username="s109-external-account")
    target.manager_access = grant_project_access(
        actor=target.grantor, user=target.manager, project=target.project, role=ProjectAccess.Role.MANAGER,
    )
    grant_project_access(
        actor=target.grantor, user=target.second_manager, project=target.project, role=ProjectAccess.Role.MANAGER,
    )
    grant_project_access(
        actor=target.grantor, user=target.other_manager, project=target.other_project, role=ProjectAccess.Role.MANAGER,
    )
    post_opening_stock(balance_id=target.balance.pk, quantity=OPENING_QUANTITY)


class StockIssueAssertions:
    def draft(self, number, rows, *, work_node=None):
        issue = StockIssue.objects.create(
            project=self.project, document_no=number, work_node=work_node or self.node, created_by=self.actor,
        )
        for index, (balance, quantity) in enumerate(rows, start=1):
            StockIssueLine.objects.create(issue=issue, balance=balance, qty=Decimal(quantity), line_no=index)
        return issue

    def approve(self, issue, actor):
        return approve_stock_issue(actor=actor, project_id=self.project.pk, issue_id=issue.pk)

    def assert_draft_without_effects(self, issue):
        issue.refresh_from_db()
        self.assertEqual(issue.status, StockIssue.Status.DRAFT)
        self.assertIsNone(issue.approved_by_id)
        self.assertIsNone(issue.approved_at)
        self.assertFalse(StockMovement.objects.filter(issue_line__issue=issue).exists())
        self.assertFalse(AuditEvent.objects.filter(stock_issue=issue).exists())

    def assert_stock_quantity(self, balance, quantity):
        balance.refresh_from_db()
        self.assertEqual(balance.quantity, Decimal(quantity))

    def assert_posted_once(self, issue, actor):
        issue.refresh_from_db()
        self.assertEqual(issue.status, StockIssue.Status.APPROVED)
        self.assertEqual(issue.approved_by, actor)
        self.assertIsNotNone(issue.approved_at)
        lines = list(issue.lines.order_by("line_no"))
        movements = StockMovement.objects.filter(issue_line__issue=issue).order_by("issue_line__line_no")
        self.assertEqual(list(movements.values_list("issue_line_id", "balance_id", "event_kind", "signed_qty")), [
            (line.pk, line.balance_id, StockMovement.EventKind.ISSUE, -line.qty) for line in lines
        ])
        event = AuditEvent.objects.get(stock_issue=issue)
        self.assertEqual((event.project, event.actor, event.event_type, event.prior_state, event.next_state), (
            self.project, actor, AuditEvent.EventType.APPROVED, "DRAFT", "APPROVED",
        ))
        self.assertEqual(event.occurred_at, issue.approved_at)


@override_settings(PASSWORD_HASHERS=TEST_HASHERS)
class StockIssueServiceTests(StockIssueAssertions, TestCase):
    fixtures = ["s1_03_demo"]

    @classmethod
    def setUpTestData(cls):
        seed_approval(cls)

    def test_approval_rejects_combined_shortage_for_two_individually_valid_lines(self):
        issue = self.draft("S109-SHORTAGE", [(self.balance, "6.0000"), (self.balance, "5.0000")])
        with self.assertRaises(InsufficientStock):
            self.approve(issue, self.manager)
        self.assert_draft_without_effects(issue)
        self.assert_stock_quantity(self.balance, "10.0000")

    def test_approval_posts_two_lines_against_the_same_balance_once(self):
        issue = self.draft("S109-TWO-LINES", [(self.balance, "4.0000"), (self.balance, "3.0000")])
        self.assertEqual(self.approve(issue, self.manager), issue)
        self.assert_posted_once(issue, self.manager)
        self.assert_stock_quantity(self.balance, "3.0000")

    def test_external_account_and_manager_from_other_project_cannot_post(self):
        issue = self.draft("S109-NO-ACCESS", [(self.balance, "3.0000")])
        ProjectAccess.objects.create(
            user=self.outsider, project=self.project, role=ProjectAccess.Role.MANAGER,
            granted_by=self.grantor, granted_at=timezone.now(),
        )
        with self.assertRaises(PermissionDenied):
            self.approve(issue, self.outsider)
        with self.assertRaises(Http404):
            self.approve(issue, self.other_manager)
        self.assert_draft_without_effects(issue)
        self.assert_stock_quantity(self.balance, "10.0000")

    def test_employee_with_project_access_but_without_approval_role_cannot_post(self):
        operator = make_employee("s109-operator")
        grant_project_access(
            actor=self.grantor, user=operator, project=self.project, role=ProjectAccess.Role.OPERATOR,
        )
        issue = self.draft("S109-OPERATOR", [(self.balance, "3.0000")])
        with self.assertRaises(PermissionDenied):
            self.approve(issue, operator)
        self.assert_draft_without_effects(issue)
        self.assert_stock_quantity(self.balance, "10.0000")

    def test_nonexecution_and_archived_work_nodes_cannot_post(self):
        scope = WorkNode.objects.get(project=self.project, node_type=WorkNode.NodeType.SCOPE)
        scope_issue = self.draft("S109-SCOPE", [(self.balance, "3.0000")], work_node=scope)
        archived_issue = self.draft("S109-ARCHIVED-NODE", [(self.balance, "3.0000")])
        WorkNode.objects.filter(pk=self.node.pk).update(status=Project.Status.ARCHIVED)
        for issue in (scope_issue, archived_issue):
            with self.subTest(issue=issue.pk), self.assertRaises(ValidationError):
                self.approve(issue, self.manager)
            self.assert_draft_without_effects(issue)
        self.assert_stock_quantity(self.balance, "10.0000")

    def test_general_balance_and_other_project_balance_are_rejected(self):
        company = Party.objects.get(identity_kind=Party.Kind.COMPANY)
        _, general = ensure_general_stock_at_location(
            owner_id=company.pk, material_id=self.pool.material_id, warehouse_id=self.balance.warehouse_id,
        )
        foreign_pool = ValuationPool.objects.create(
            scope_type=ValuationPool.ScopeType.PROJECT_STOCK,
            project=self.other_project, owner=self.other_project.owner_party, material=self.pool.material,
        )
        foreign = StockLocationBalance.objects.create(pool=foreign_pool, warehouse=self.balance.warehouse)
        for index, ineligible_balance in enumerate((general, foreign), start=1):
            post_opening_stock(balance_id=ineligible_balance.pk, quantity=OPENING_QUANTITY)
            issue = self.draft(f"S109-INELIGIBLE-{index}", [
                (self.balance, "1.0000"), (ineligible_balance, "2.0000"),
            ])
            with self.subTest(balance=ineligible_balance.pk), self.assertRaises(ValidationError):
                self.approve(issue, self.manager)
            self.assert_draft_without_effects(issue)
            self.assert_stock_quantity(ineligible_balance, "10.0000")
        self.assert_stock_quantity(self.balance, "10.0000")

    def test_issue_with_no_lines_is_rejected_without_changes(self):
        issue = self.draft("S109-EMPTY", [])
        with self.assertRaises(ValidationError):
            self.approve(issue, self.manager)
        self.assert_draft_without_effects(issue)
        self.assert_stock_quantity(self.balance, "10.0000")
