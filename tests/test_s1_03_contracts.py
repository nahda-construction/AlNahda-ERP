from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.contracts.models import ContractComponent, ContractRevision, OwnerContract
from apps.contracts.services import approve_revision
from apps.projects.models import Project


class ContractSchemaTests(TestCase):
    fixtures = ["s1_03_demo"]

    def assert_constraint_rejected(self, name, action):
        with self.assertRaises(IntegrityError) as error, transaction.atomic():
            action()
        self.assertEqual(error.exception.__cause__.diag.constraint_name, name)

    def test_project_has_only_one_owner_contract(self):
        project = Project.objects.get(code="DEMO-A")
        self.assertEqual(project.ownercontract.project.owner_party_id, project.owner_party_id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OwnerContract.objects.create(project=project, code="SECOND")

    def test_pricing_methods_preserve_their_independent_values(self):
        a = ContractComponent.objects.get(revision__contract__code="DEMO-C-A", component_code="MGMT")
        b = ContractRevision.objects.get(contract__code="DEMO-C-B", revision_no=2)
        units = b.components.get(component_code="UNITS")
        stage = b.components.get(component_code="STAGE")
        self.assertEqual(a.fee_rate, Decimal("17.0000"))
        self.assertIsNone(units.fee_rate)
        self.assertEqual(units.unit_price * 2, Decimal("550.00"))
        self.assertEqual(stage.stage_amount, Decimal("5000.00"))

    def test_postgresql_rejects_invalid_pricing_and_duplicate_codes(self):
        revision = ContractRevision.objects.get(contract__code="DEMO-C-B", revision_no=2)
        for values in (
            {},
            {"unit_price": Decimal("-1")},
            {"unit_price": Decimal("2"), "stage_amount": Decimal("1")},
        ):
            with self.subTest(fields=values):
                self.assert_constraint_rejected(
                    "contract_component_pricing_valid",
                    lambda: revision.components.create(
                        component_code="INVALID", name="Invalid", pricing_method="UNIT_PRICE", **values,
                    ),
                )
        self.assert_constraint_rejected(
            "contract_component_code_unique",
            lambda: revision.components.create(
                component_code="UNITS", name="Duplicate", pricing_method="UNIT_PRICE", unit_price=Decimal("2"),
            ),
        )
        self.assert_constraint_rejected(
            "contract_revision_number_unique",
            lambda: ContractRevision.objects.create(
                contract=revision.contract, revision_no=2, valid_from=date(2026, 2, 1),
            ),
        )

    def test_approval_preserves_two_rows_and_blocks_changes_to_history(self):
        approver = get_user_model().objects.create_user(username="s103-test-approver")
        old = ContractRevision.objects.get(contract__code="DEMO-C-B", revision_no=1)
        new = ContractRevision.objects.get(contract__code="DEMO-C-B", revision_no=2)
        approve_revision(old.pk, approved_by=approver)
        approve_revision(new.pk, approved_by=approver)

        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(old.status, ContractRevision.Status.SUPERSEDED)
        self.assertEqual(new.status, ContractRevision.Status.APPROVED)
        self.assertEqual(new.approved_by_id, approver.pk)
        self.assertIsNotNone(new.approved_at)
        old_units = old.components.get(component_code="UNITS")
        new_units = new.components.get(component_code="UNITS")
        self.assertNotEqual(old_units.pk, new_units.pk)
        self.assertEqual(old_units.component_code, new_units.component_code)
        self.assertEqual(old_units.unit_price, Decimal("250.0000"))
        self.assertEqual(new_units.unit_price, Decimal("275.0000"))
        self.assertEqual(new.components.get(component_code="STAGE").stage_amount, Decimal("5000.00"))

        for component in (old_units, new_units):
            with self.subTest(revision=component.revision_id):
                self.assert_constraint_rejected(
                    "contract_component_draft_only",
                    lambda: ContractComponent.objects.filter(pk=component.pk).update(unit_price=Decimal("1")),
                )
        self.assert_constraint_rejected(
            "contract_component_draft_only",
            lambda: new.components.create(
                component_code="EXTRA", name="New terms", pricing_method="UNIT_PRICE", unit_price=Decimal("1"),
            ),
        )
        self.assert_constraint_rejected("contract_component_draft_only", lambda: new_units.delete())
        self.assert_constraint_rejected(
            "contract_revision_immutable",
            lambda: ContractRevision.objects.filter(pk=new.pk).update(valid_from=date(2026, 9, 1)),
        )
        with self.assertRaises(ValidationError):
            approve_revision(new.pk, approved_by=approver)
