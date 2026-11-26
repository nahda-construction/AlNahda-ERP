from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from queue import Queue
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections
from django.test import TransactionTestCase

from apps.inventory.models import (
    BALANCE_UNIQUE_CONSTRAINT,
    POOL_UNIQUE_CONSTRAINT,
    StockLocationBalance,
    ValuationPool,
    Warehouse,
)
from apps.inventory.services import ensure_general_stock_at_location
from apps.projects.models import Party

from .test_s1_07_inventory_constraints import create_material, general_pool_key


WAIT_TIMEOUT = 8


def backend_details():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid(), current_setting('transaction_isolation')")
        return cursor.fetchone()


class InventoryConcurrencyTests(TransactionTestCase):
    fixtures = ["s1_03_demo"]

    def setUp(self):
        self.company = Party.objects.get(identity_kind=Party.Kind.COMPANY)
        self.material = create_material("RACE-MATERIAL")
        self.first_warehouse = Warehouse.objects.create(code="RACE-SITE-A", name="Synthetic site A")
        self.second_warehouse = Warehouse.objects.create(code="RACE-SITE-B", name="Synthetic site B")
        self.pool_key = general_pool_key(self.company, self.material)

    def ensure_stock(self, warehouse):
        return ensure_general_stock_at_location(
            owner_id=self.company.pk, material_id=self.material.pk, warehouse_id=warehouse.pk,
        )

    def ensure_on_own_connection(self, warehouse):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout TO '15s'")
            return self.ensure_stock(warehouse)
        finally:
            connections.close_all()

    def assert_second_insert_waits(self, first_pid, second_pid, model):
        observer_pid, _ = backend_details()
        self.assertEqual(len({first_pid, second_pid, observer_pid}), 3)
        deadline = monotonic() + WAIT_TIMEOUT
        while monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_blocking_pids(pid), query FROM pg_stat_activity WHERE pid = %s",
                    [second_pid],
                )
                blockers, query = cursor.fetchone()
            if first_pid in blockers:
                self.assertIn("INSERT INTO", query)
                self.assertIn(model._meta.db_table, query)
                return
            sleep(0.02)
        self.fail(f"PostgreSQL did not report insert by {second_pid} waiting on {first_pid}")

    def race(self, model, identity, second_warehouse, *, constraint, on_wait=None, expected_error=None):
        attempts = Queue()
        collisions = Queue()
        inserted = Event()
        release_first = Event()
        actual_insert = model.objects.create

        def pause_after_first_insert(**fields):
            attempts.put(backend_details())
            try:
                row = actual_insert(**fields)
            except IntegrityError as exc:
                cause = exc.__cause__
                collisions.put((cause.sqlstate, cause.diag.constraint_name))
                raise
            if not inserted.is_set():
                inserted.set()
                if not release_first.wait(timeout=WAIT_TIMEOUT * 2):
                    raise AssertionError("First insert did not receive its commit signal")
            return row

        with (
            patch.object(model.objects, "create", side_effect=pause_after_first_insert),
            patch.object(model.objects, "get", wraps=model.objects.get) as replay,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(self.ensure_on_own_connection, self.first_warehouse)
            try:
                first_pid, first_isolation = attempts.get(timeout=WAIT_TIMEOUT)
                self.assertTrue(inserted.wait(timeout=WAIT_TIMEOUT), "First request did not insert its row")
                second = executor.submit(self.ensure_on_own_connection, second_warehouse)
                second_pid, second_isolation = attempts.get(timeout=WAIT_TIMEOUT)
                self.assertEqual((first_isolation, second_isolation), ("read committed", "read committed"))
                self.assert_second_insert_waits(first_pid, second_pid, model)
                self.assertFalse(model.objects.filter(**identity).exists())
                self.assertFalse(second.done())
                if on_wait is not None:
                    on_wait()
            finally:
                release_first.set()
            first_result = first.result(timeout=WAIT_TIMEOUT)
            if expected_error is None:
                second_result = second.result(timeout=WAIT_TIMEOUT)
                replay.assert_any_call(**identity)
            else:
                with self.assertRaises(expected_error):
                    second.result(timeout=WAIT_TIMEOUT)
                second_result = None
        self.assertEqual(collisions.get_nowait(), ("23505", constraint))
        self.assertTrue(collisions.empty())
        return first_result, second_result

    def test_competing_locations_commit_one_general_pool_and_two_independent_zero_balances(self):
        (first_pool, first_balance), (second_pool, second_balance) = self.race(
            ValuationPool, self.pool_key, self.second_warehouse, constraint=POOL_UNIQUE_CONSTRAINT,
        )
        self.assertEqual(first_pool.pk, second_pool.pk)
        self.assertEqual(list(ValuationPool.objects.filter(**self.pool_key).values_list("pk", flat=True)), [first_pool.pk])
        self.assertNotEqual(first_balance.pk, second_balance.pk)
        self.assertEqual((first_balance.warehouse_id, second_balance.warehouse_id), (
            self.first_warehouse.pk, self.second_warehouse.pk,
        ))
        self.assertCountEqual(
            StockLocationBalance.objects.filter(pool=first_pool).values_list("id", "warehouse_id", "stock_state", "quantity"),
            [
                (first_balance.pk, self.first_warehouse.pk, "AVAILABLE", Decimal("0.0000")),
                (second_balance.pk, self.second_warehouse.pk, "AVAILABLE", Decimal("0.0000")),
            ],
        )

    def test_waiting_pool_request_rechecks_warehouse_deactivated_before_the_winners_commit(self):
        def deactivate_second_warehouse():
            Warehouse.objects.filter(pk=self.second_warehouse.pk).update(active=False)

        (pool, balance), _ = self.race(
            ValuationPool, self.pool_key, self.second_warehouse, constraint=POOL_UNIQUE_CONSTRAINT,
            on_wait=deactivate_second_warehouse, expected_error=ValidationError,
        )
        self.assertEqual(list(StockLocationBalance.objects.filter(pool=pool).values_list("pk", flat=True)), [balance.pk])
        self.assertEqual(balance.warehouse_id, self.first_warehouse.pk)

    def test_competing_requests_to_same_location_return_the_only_committed_balance(self):
        pool = ValuationPool.objects.create(**self.pool_key, general_owner_kind=Party.Kind.COMPANY)
        key = {"pool_id": pool.pk, "warehouse_id": self.first_warehouse.pk, "stock_state": "AVAILABLE"}
        (first_pool, first_balance), (second_pool, second_balance) = self.race(
            StockLocationBalance, key, self.first_warehouse, constraint=BALANCE_UNIQUE_CONSTRAINT,
        )
        self.assertEqual(first_pool, pool)
        self.assertEqual(second_pool, pool)
        self.assertEqual(first_balance.pk, second_balance.pk)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, quantity FROM inventory_stocklocationbalance
                WHERE pool_id = %s AND warehouse_id = %s AND stock_state = %s
                """,
                [pool.pk, self.first_warehouse.pk, "AVAILABLE"],
            )
            self.assertEqual(cursor.fetchall(), [(first_balance.pk, Decimal("0.0000"))])

    def test_committed_positive_balance_is_returned_without_overwriting_quantity(self):
        pool = ValuationPool.objects.create(**self.pool_key, general_owner_kind=Party.Kind.COMPANY)
        quantity = Decimal("17.2500")
        initial = StockLocationBalance.objects.create(pool=pool, warehouse=self.first_warehouse, quantity=quantity)
        same_pool, same_balance = self.ensure_stock(self.first_warehouse)
        self.assertEqual(same_pool, pool)
        self.assertEqual(same_balance.pk, initial.pk)
        with connection.cursor() as cursor:
            cursor.execute("SELECT quantity FROM inventory_stocklocationbalance WHERE id = %s", [initial.pk])
            self.assertEqual(cursor.fetchone(), (quantity,))
        self.assertEqual(StockLocationBalance.objects.count(), 1)

    def assert_original_insert_error(self, model, changes, *, sqlstate, constraint):
        actual_insert = model.objects.create
        errors = []

        def insert_with_unrelated_violation(**fields):
            self.assertTrue(connection.in_atomic_block)
            self.assertIsNotNone(connection.savepoint_ids[-1])
            try:
                return actual_insert(**(fields | changes))
            except IntegrityError as exc:
                errors.append(exc)
                raise

        with (
            patch.object(model.objects, "create", side_effect=insert_with_unrelated_violation),
            patch.object(model.objects, "get", wraps=model.objects.get) as replay,
            self.assertRaises(IntegrityError) as capture,
        ):
            self.ensure_stock(self.first_warehouse)
        self.assertEqual(len(errors), 1)
        self.assertIs(capture.exception, errors[0])
        self.assertEqual(capture.exception.__cause__.sqlstate, sqlstate)
        self.assertEqual(capture.exception.__cause__.diag.constraint_name, constraint)
        replay.assert_not_called()
        self.assertFalse(ValuationPool.objects.filter(**self.pool_key).exists())

    def test_pool_insert_reraises_other_unique_violation_without_replaying(self):
        different = create_material("OTHER-POOL-MATERIAL")
        pool = ValuationPool.objects.create(
            **general_pool_key(self.company, different), general_owner_kind=Party.Kind.COMPANY,
        )
        self.assert_original_insert_error(
            ValuationPool, {"id": pool.pk}, sqlstate="23505", constraint="inventory_valuationpool_pkey",
        )
        self.assertEqual(list(ValuationPool.objects.values_list("pk", flat=True)), [pool.pk])
        self.assertFalse(StockLocationBalance.objects.exists())

    def test_balance_insert_reraises_other_unique_violation_and_rolls_back_new_pool(self):
        different = create_material("OTHER-BALANCE-MATERIAL")
        pool = ValuationPool.objects.create(
            **general_pool_key(self.company, different), general_owner_kind=Party.Kind.COMPANY,
        )
        balance = StockLocationBalance.objects.create(pool=pool, warehouse=self.second_warehouse)
        self.assert_original_insert_error(
            StockLocationBalance, {"id": balance.pk}, sqlstate="23505",
            constraint="inventory_stocklocationbalance_pkey",
        )
        self.assertEqual(list(ValuationPool.objects.values_list("pk", flat=True)), [pool.pk])
        self.assertEqual(list(StockLocationBalance.objects.values_list("pk", flat=True)), [balance.pk])

    def test_balance_insert_reraises_real_check_violation_and_rolls_back_new_pool(self):
        self.assert_original_insert_error(
            StockLocationBalance, {"quantity": Decimal("-0.0001")}, sqlstate="23514",
            constraint="inventory_balance_quantity_nonnegative",
        )
        self.assertFalse(ValuationPool.objects.exists())
        self.assertFalse(StockLocationBalance.objects.exists())
