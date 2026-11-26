from collections import defaultdict
from decimal import Decimal, localcontext

from django.core.exceptions import ValidationError
from django.core.validators import DecimalValidator
from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils import timezone

from apps.access_control.policies import Action
from apps.projects.models import Party
from apps.test_transactions.models import AuditEvent
from apps.test_transactions.services import eligible_work_nodes, require_project

from .models import (
    BALANCE_UNIQUE_CONSTRAINT,
    POOL_UNIQUE_CONSTRAINT,
    Material,
    StockIssue,
    StockLocationBalance,
    StockMovement,
    ValuationPool,
    Warehouse,
)


class StockIssueConflict(ValidationError):
    pass


class InsufficientStock(ValidationError):
    pass


def _validate_references(*, owner_id, material_id, warehouse_id, owner_kind=None):
    owners = Party.objects.filter(pk=owner_id, active=True)
    if owner_kind is not None:
        owners = owners.filter(identity_kind=owner_kind)
    if not owners.exists():
        raise ValidationError("Stock requires an active eligible owner.")
    if not Material.objects.filter(pk=material_id, active=True).exists():
        raise ValidationError("Stock requires an active material.")
    if not Warehouse.objects.filter(pk=warehouse_id, active=True).exists():
        raise ValidationError("Stock requires an active warehouse.")


def _validate_active_pool(pool):
    if pool.status != ValuationPool.Status.ACTIVE:
        raise ValidationError("The valuation pool is archived.")


def _is_unique_conflict(error, constraint_name):
    cause = error.__cause__
    return (
        getattr(cause, "sqlstate", None) == "23505"
        and getattr(getattr(cause, "diag", None), "constraint_name", None) == constraint_name
    )


def ensure_general_stock_at_location(
    *, owner_id, material_id, warehouse_id,
) -> tuple[ValuationPool, StockLocationBalance]:
    references = {
        "owner_id": owner_id,
        "material_id": material_id,
        "warehouse_id": warehouse_id,
        "owner_kind": Party.Kind.COMPANY,
    }
    pool_key = {
        "scope_type": ValuationPool.ScopeType.GENERAL_COMPANY,
        "project_id": None,
        "owner_id": owner_id,
        "material_id": material_id,
    }

    with transaction.atomic():
        _validate_references(**references)
        try:
            with transaction.atomic():
                pool = ValuationPool.objects.create(**pool_key, general_owner_kind=Party.Kind.COMPANY)
        except IntegrityError as exc:
            if not _is_unique_conflict(exc, POOL_UNIQUE_CONSTRAINT):
                raise
            pool = ValuationPool.objects.get(**pool_key)

        _validate_references(**references)
        _validate_active_pool(pool)
        balance_key = {
            "pool_id": pool.pk,
            "warehouse_id": warehouse_id,
            "stock_state": StockLocationBalance.StockState.AVAILABLE,
        }
        try:
            with transaction.atomic():
                balance = StockLocationBalance.objects.create(**balance_key, quantity=Decimal("0.0000"))
        except IntegrityError as exc:
            if not _is_unique_conflict(exc, BALANCE_UNIQUE_CONSTRAINT):
                raise
            balance = StockLocationBalance.objects.get(**balance_key)

        # A competing insert can delay either step. Recheck persisted eligibility before returning.
        _validate_references(**references)
        pool = ValuationPool.objects.get(pk=balance.pool_id, **pool_key)
        _validate_active_pool(pool)
        return pool, balance


def _validate_available_balance(balance):
    if balance.stock_state != StockLocationBalance.StockState.AVAILABLE:
        raise ValidationError("Only available stock can be posted.")
    pool = balance.pool
    _validate_active_pool(pool)
    _validate_references(
        owner_id=pool.owner_id, material_id=pool.material_id, warehouse_id=balance.warehouse_id,
    )


def post_opening_stock(*, balance_id, quantity: Decimal) -> StockMovement:
    if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
        raise ValidationError("Opening quantity must be a positive finite Decimal.")
    DecimalValidator(14, 4)(quantity)

    with transaction.atomic():
        try:
            balance = StockLocationBalance.objects.select_for_update(of=("self",)).get(pk=balance_id)
        except StockLocationBalance.DoesNotExist:
            raise ValidationError("The stock balance does not exist.") from None
        _validate_available_balance(balance)
        if balance.quantity != 0 or StockMovement.objects.filter(
            balance=balance, event_kind=StockMovement.EventKind.OPENING,
        ).exists():
            raise ValidationError("Opening stock requires a zero balance without an opening movement.")

        movement = StockMovement.objects.create(
            balance=balance, event_kind=StockMovement.EventKind.OPENING, signed_qty=quantity,
        )
        balance.quantity = quantity
        balance.save(update_fields=("quantity",))
        return movement


def _issue_quantities(lines):
    quantities = defaultdict(Decimal)
    with localcontext(prec=28):
        for line in lines:
            quantities[line.balance_id] += line.qty
    return quantities


def _post_stock_issue(*, issue, actor, lines, balances_by_id) -> StockIssue:
    """Post validated lines using the issue and balance locks held by the caller."""
    if not transaction.get_connection().in_atomic_block:
        raise transaction.TransactionManagementError("Stock posting requires an active transaction.")

    lines = tuple(lines)
    quantities = _issue_quantities(lines)
    for line in lines:
        StockMovement.objects.create(
            balance_id=line.balance_id, issue_line=line, event_kind=StockMovement.EventKind.ISSUE,
            signed_qty=line.qty.copy_negate(),
        )
    with localcontext(prec=28):
        for balance_id, quantity in quantities.items():
            balance = balances_by_id[balance_id]
            balance.quantity -= quantity
            balance.save(update_fields=("quantity",))

    issue.status = StockIssue.Status.APPROVED
    issue.approved_by = actor
    issue.approved_at = timezone.now()
    issue.save(update_fields=("status", "approved_by", "approved_at"))
    AuditEvent.objects.create(
        project_id=issue.project_id, actor=actor, occurred_at=issue.approved_at,
        event_type=AuditEvent.EventType.APPROVED, stock_issue=issue,
        prior_state=StockIssue.Status.DRAFT, next_state=StockIssue.Status.APPROVED,
    )
    return issue


def _validate_issue_for_approval(actor, issue):
    require_project(actor, issue.project_id, Action.APPROVE_TEST_STOCK_ISSUE, for_write=True)
    if issue.status != StockIssue.Status.DRAFT:
        raise StockIssueConflict("Only a draft stock issue can be approved.")
    if not eligible_work_nodes(issue.project_id).filter(pk=issue.work_node_id).exists():
        raise ValidationError("The stock issue requires an active item or activity in its project.")


def approve_stock_issue(*, actor, project_id, issue_id) -> StockIssue:
    require_project(actor, project_id, Action.APPROVE_TEST_STOCK_ISSUE, for_write=True)

    with transaction.atomic():
        try:
            issue = StockIssue.objects.select_for_update(of=("self",)).get(pk=issue_id, project_id=project_id)
        except StockIssue.DoesNotExist:
            raise Http404 from None
        _validate_issue_for_approval(actor, issue)

        # The locked issue also prevents concurrent edits to its lines.
        lines = tuple(issue.lines.order_by("line_no"))
        if not lines:
            raise ValidationError("A stock issue needs at least one line before approval.")
        quantities = _issue_quantities(lines)
        locked_balances = (
            StockLocationBalance.objects.filter(pk__in=quantities)
            .order_by("pk")
            .select_for_update(of=("self",))
        )
        balances_by_id = {balance.pk: balance for balance in locked_balances}

        _validate_issue_for_approval(actor, issue)
        if len(balances_by_id) != len(quantities):
            raise ValidationError("The stock issue refers to a missing balance.")
        for balance in balances_by_id.values():
            pool = balance.pool
            if pool.scope_type != ValuationPool.ScopeType.PROJECT_STOCK or pool.project_id != issue.project_id:
                raise ValidationError("Stock must belong to the stock issue's project.")
            _validate_available_balance(balance)
        if any(quantity > balances_by_id[balance_id].quantity for balance_id, quantity in quantities.items()):
            raise InsufficientStock("The stock issue exceeds the available balance.")

        return _post_stock_issue(issue=issue, actor=actor, lines=lines, balances_by_id=balances_by_id)
