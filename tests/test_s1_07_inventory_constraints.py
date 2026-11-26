from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from psycopg import sql

from apps.inventory.models import (
    BALANCE_UNIQUE_CONSTRAINT,
    POOL_UNIQUE_CONSTRAINT,
    Material,
    StockLocationBalance,
    ValuationPool,
    Warehouse,
)
from apps.inventory.services import ensure_general_stock_at_location
from apps.projects.models import Party, Project


def general_pool_key(owner, material):
    return {
        "scope_type": ValuationPool.ScopeType.GENERAL_COMPANY,
        "project_id": None,
        "owner_id": owner.pk,
        "material_id": material.pk,
    }


def create_material(code):
    return Material.objects.create(code=code, name=code, base_unit="DEMO_EACH", valuation_method="DEMO_METHOD")


class InventoryConstraintTests(TestCase):
    fixtures = ["s1_03_demo"]

    @classmethod
    def setUpTestData(cls):
        cls.company = Party.objects.get(identity_kind=Party.Kind.COMPANY)
        cls.project_a = Project.objects.get(code="DEMO-A")
        cls.project_b = Project.objects.get(code="DEMO-B")
        cls.external = cls.project_b.owner_party
        cls.material = create_material("SYNTHETIC-MATERIAL")
        cls.other_material = create_material("OTHER-SYNTHETIC-MATERIAL")
        cls.warehouse = Warehouse.objects.create(code="SYNTHETIC-WAREHOUSE", name="Synthetic warehouse")
        cls.other_warehouse = Warehouse.objects.create(code="OTHER-WAREHOUSE", name="Other synthetic warehouse")

    def execute(self, statement, parameters=()):
        with connection.cursor() as cursor:
            cursor.execute(statement, parameters)

    def insert(self, model, values):
        fields = {"id": uuid4(), **values}
        columns = sql.SQL(", ").join(map(sql.Identifier, fields))
        placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in fields)
        self.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(model._meta.db_table), columns, placeholders,
            ),
            tuple(fields.values()),
        )
        return fields["id"]

    def insert_pool(self, **overrides):
        fields = {
            **general_pool_key(self.company, self.material),
            "general_owner_kind": Party.Kind.COMPANY,
            "status": ValuationPool.Status.ACTIVE,
            **overrides,
        }
        return self.insert(ValuationPool, fields)

    def insert_balance(self, pool_id, **overrides):
        return self.insert(StockLocationBalance, {
            "pool_id": pool_id,
            "warehouse_id": self.warehouse.pk,
            "stock_state": StockLocationBalance.StockState.AVAILABLE,
            "quantity": Decimal("0.0000"),
            **overrides,
        })

    def assert_pg_rejects(self, operation, sqlstate, *, constraint=None, column=None):
        with self.assertRaises(IntegrityError) as captured, transaction.atomic():
            operation()
            self.execute("SET CONSTRAINTS ALL IMMEDIATE")
        original = captured.exception.__cause__
        self.assertEqual(original.sqlstate, sqlstate)
        if constraint is not None:
            expected = (constraint,) if isinstance(constraint, str) else constraint
            self.assertIn(original.diag.constraint_name, expected)
        if column is not None:
            self.assertEqual(original.diag.column_name, column)

    def single_column_fk(self, model, column):
        with connection.cursor() as cursor:
            definitions = connection.introspection.get_constraints(cursor, model._meta.db_table)
        names = [
            name for name, definition in definitions.items()
            if definition["foreign_key"] and definition["columns"] == [column]
        ]
        self.assertEqual(len(names), 1)
        return names[0]

    def ensure_stock(self, *, owner=None, warehouse=None):
        return ensure_general_stock_at_location(
            owner_id=(owner or self.company).pk,
            material_id=self.material.pk,
            warehouse_id=(warehouse or self.warehouse).pk,
        )

    def test_general_identity_is_unique_even_with_null_project(self):
        key = general_pool_key(self.company, self.material)
        first_id = self.insert_pool()
        self.assert_pg_rejects(self.insert_pool, "23505", constraint=POOL_UNIQUE_CONSTRAINT)
        self.assertEqual(list(ValuationPool.objects.filter(**key).values_list("id", flat=True)), [first_id])

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT idx.indnullsnotdistinct, pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_index idx ON idx.indexrelid = c.conindid
                WHERE c.conrelid = %s::regclass AND c.conname = %s
                """,
                [ValuationPool._meta.db_table, POOL_UNIQUE_CONSTRAINT],
            )
            self.assertEqual(cursor.fetchone(), (
                True, "UNIQUE NULLS NOT DISTINCT (scope_type, project_id, owner_id, material_id)",
            ))

    def test_postgresql_rejects_wrong_scope_shapes_and_forged_company_owner(self):
        project_stock = ValuationPool.ScopeType.PROJECT_STOCK
        cases = (
            {"project_id": self.project_a.pk},
            {"general_owner_kind": None},
            {"general_owner_kind": Party.Kind.EXTERNAL},
            {"scope_type": project_stock, "general_owner_kind": None},
            {"scope_type": project_stock, "project_id": self.project_a.pk},
            {"scope_type": "UNSUPPORTED"},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                self.assert_pg_rejects(
                    lambda: self.insert_pool(**fields), "23514", constraint="inventory_pool_scope_valid",
                )
        self.assert_pg_rejects(
            lambda: self.insert_pool(owner_id=self.external.pk),
            "23503", constraint="inventory_general_pool_owner_kind_fk",
        )
        self.assertFalse(ValuationPool.objects.exists())

    def test_identity_columns_are_required_and_project_pool_references_must_exist(self):
        valid = {
            "scope_type": ValuationPool.ScopeType.PROJECT_STOCK,
            "project_id": self.project_a.pk,
            "owner_id": self.external.pk,
            "general_owner_kind": None,
        }
        for column in ("scope_type", "owner_id", "material_id"):
            with self.subTest(required=column):
                self.assert_pg_rejects(
                    lambda: self.insert_pool(**(valid | {column: None})), "23502", column=column,
                )
        for column in ("project_id", "owner_id", "material_id"):
            with self.subTest(foreign_key=column):
                self.assert_pg_rejects(
                    lambda: self.insert_pool(**(valid | {column: uuid4()})), "23503",
                    constraint=self.single_column_fk(ValuationPool, column),
                )
        self.assertFalse(ValuationPool.objects.exists())

    def test_project_pool_allows_distinct_existing_owner_and_separates_valid_identities(self):
        self.assertNotEqual(self.external, self.project_a.owner_party)
        fields = {
            "scope_type": ValuationPool.ScopeType.PROJECT_STOCK,
            "project_id": self.project_a.pk,
            "owner_id": self.external.pk,
            "general_owner_kind": None,
        }
        self.insert_pool(**fields)
        self.assert_pg_rejects(
            lambda: self.insert_pool(**fields), "23505", constraint=POOL_UNIQUE_CONSTRAINT,
        )
        for different in (
            {"project_id": self.project_b.pk},
            {"owner_id": self.company.pk},
            {"material_id": self.other_material.pk},
        ):
            self.insert_pool(**(fields | different))
        self.insert_pool()
        self.assertEqual(ValuationPool.objects.count(), 5)

    def test_active_external_owner_is_denied_before_replaying_a_pool(self):
        pool, balance = self.ensure_stock()
        self.assertTrue(self.external.active)
        with (
            patch.object(ValuationPool.objects, "get", wraps=ValuationPool.objects.get) as pool_lookup,
            patch.object(StockLocationBalance.objects, "get", wraps=StockLocationBalance.objects.get) as balance_lookup,
            self.assertRaises(ValidationError),
        ):
            self.ensure_stock(owner=self.external)
        pool_lookup.assert_not_called()
        balance_lookup.assert_not_called()
        self.assertEqual(list(ValuationPool.objects.values_list("id", flat=True)), [pool.pk])
        self.assertEqual(list(StockLocationBalance.objects.values_list("id", flat=True)), [balance.pk])

    def test_catalog_codes_and_location_balance_identity_are_unique(self):
        self.assert_pg_rejects(
            lambda: create_material(self.material.code), "23505", constraint="uq_inventory_material_code",
        )
        self.assert_pg_rejects(
            lambda: Warehouse.objects.create(code=self.warehouse.code, name="Duplicate"),
            "23505", constraint="uq_inventory_warehouse_code",
        )
        pool_id = self.insert_pool()
        balance_id = self.insert_balance(pool_id)
        self.assert_pg_rejects(
            lambda: self.insert_balance(pool_id), "23505", constraint=BALANCE_UNIQUE_CONSTRAINT,
        )
        second_id = self.insert_balance(pool_id, warehouse_id=self.other_warehouse.pk)
        self.assertCountEqual(StockLocationBalance.objects.values_list("id", flat=True), [balance_id, second_id])

    def test_postgresql_rejects_invalid_balance_state_and_quantities(self):
        pool_id = self.insert_pool()
        self.assert_pg_rejects(
            lambda: self.insert_balance(pool_id, stock_state="BLOCKED"),
            "23514", constraint="inventory_balance_stock_state_valid",
        )
        cases = (
            (Decimal("-0.0001"), "inventory_balance_quantity_nonnegative"),
            (Decimal("NaN"), "inventory_balance_quantity_not_nan"),
        )
        for quantity, constraint in cases:
            with self.subTest(quantity=quantity):
                self.assert_pg_rejects(
                    lambda: self.insert_balance(pool_id, quantity=quantity), "23514", constraint=constraint,
                )
        for quantity in (Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(quantity=quantity):
                with self.assertRaises(DataError) as captured, transaction.atomic():
                    self.insert_balance(pool_id, quantity=quantity)
                self.assertEqual(captured.exception.__cause__.sqlstate, "22003")
        for column in ("pool_id", "warehouse_id", "stock_state", "quantity"):
            with self.subTest(required=column):
                fields = {"pool_id": pool_id, column: None}
                self.assert_pg_rejects(lambda: self.insert_balance(**fields), "23502", column=column)
        self.assertFalse(StockLocationBalance.objects.exists())

    def test_references_are_protected_in_django_and_postgresql(self):
        general_id = self.insert_pool()
        self.insert_balance(general_id)
        general = ValuationPool.objects.get(pk=general_id)
        project = Project.objects.create(code="STOCK-ONLY", name="Synthetic stock only", owner_party=self.external)
        project_pool_owner = Party.objects.create(
            code="STOCK-OWNER-ONLY", name="Synthetic stock owner", identity_kind=Party.Kind.EXTERNAL,
        )
        self.insert_pool(
            scope_type=ValuationPool.ScopeType.PROJECT_STOCK, project_id=project.pk,
            owner_id=project_pool_owner.pk, general_owner_kind=None,
        )

        references = (
            (self.company, ValuationPool, "owner_id"),
            (project_pool_owner, ValuationPool, "owner_id"),
            (project, ValuationPool, "project_id"),
            (self.material, ValuationPool, "material_id"),
            (general, StockLocationBalance, "pool_id"),
            (self.warehouse, StockLocationBalance, "warehouse_id"),
        )
        for target, child, column in references:
            with self.subTest(reference=target._meta.label, column=column):
                with self.assertRaises(ProtectedError):
                    target.delete()
                constraints = (self.single_column_fk(child, column),)
                if target == self.company:
                    constraints += ("inventory_general_pool_owner_kind_fk",)
                self.assert_pg_rejects(
                    lambda: self.execute(
                        sql.SQL("DELETE FROM {} WHERE id = %s").format(sql.Identifier(target._meta.db_table)),
                        [target.pk],
                    ),
                    "23503", constraint=constraints,
                )
        self.assert_pg_rejects(
            lambda: self.execute(
                "UPDATE projects_party SET identity_kind = %s WHERE id = %s",
                [Party.Kind.EXTERNAL, self.company.pk],
            ),
            "23503", constraint="inventory_general_pool_owner_kind_fk",
        )

    def test_disabled_references_and_archived_pool_reject_use_and_keep_prior_rows_readable(self):
        pool, balance = self.ensure_stock()
        for target, changes, requested_warehouse in (
            (self.company, {"active": False}, self.other_warehouse),
            (self.material, {"active": False}, self.other_warehouse),
            (self.warehouse, {"active": False}, self.warehouse),
            (pool, {"status": ValuationPool.Status.ARCHIVED}, self.other_warehouse),
        ):
            with self.subTest(reference=target._meta.label), transaction.atomic():
                type(target).objects.filter(pk=target.pk).update(**changes)
                with self.assertRaises(ValidationError):
                    self.ensure_stock(warehouse=requested_warehouse)
                self.assertEqual(list(ValuationPool.objects.values_list("id", flat=True)), [pool.pk])
                self.assertEqual(list(StockLocationBalance.objects.values_list("id", flat=True)), [balance.pk])
                transaction.set_rollback(True)
