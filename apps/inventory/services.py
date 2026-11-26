from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.projects.models import Party

from .models import (
    BALANCE_UNIQUE_CONSTRAINT,
    POOL_UNIQUE_CONSTRAINT,
    Material,
    StockLocationBalance,
    ValuationPool,
    Warehouse,
)


def _validate_references(*, owner_id, material_id, warehouse_id):
    if not Party.objects.filter(pk=owner_id, identity_kind=Party.Kind.COMPANY, active=True).exists():
        raise ValidationError("General stock requires an active company owner.")
    if not Material.objects.filter(pk=material_id, active=True).exists():
        raise ValidationError("General stock requires an active material.")
    if not Warehouse.objects.filter(pk=warehouse_id, active=True).exists():
        raise ValidationError("General stock requires an active warehouse.")


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
    references = {"owner_id": owner_id, "material_id": material_id, "warehouse_id": warehouse_id}
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
