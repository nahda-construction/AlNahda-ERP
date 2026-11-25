from decimal import Decimal

from django import forms
from django.contrib.auth.forms import AuthenticationForm

from apps.access_control.policies import is_internal_employee

from . import services


class EmployeeAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not is_internal_employee(user):
            raise self.get_invalid_login_error()


class TransactionForm(forms.Form):
    title = forms.CharField(label="عنوان المعاملة", max_length=160)
    attachment = forms.FileField(
        label="مرفق PDF اصطناعي (حتى 5 MiB)",
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,application/pdf"}),
    )


class WorkNodeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, node):
        return f"{node.code} — {node.name}"


class TransactionLineForm(forms.Form):
    work_node = WorkNodeChoiceField(queryset=None, label="البند أو النشاط")
    description = forms.CharField(label="الوصف", max_length=240, required=False)
    qty = forms.DecimalField(
        label="الكمية", max_digits=services.QUANTITY_DIGITS,
        decimal_places=services.INPUT_DECIMAL_PLACES, min_value=Decimal("0.0001"),
    )
    unit_price = forms.DecimalField(
        label="سعر الوحدة", max_digits=services.UNIT_PRICE_DIGITS,
        decimal_places=services.INPUT_DECIMAL_PLACES, min_value=Decimal("0"),
    )

    def __init__(self, *args, work_nodes, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["work_node"].queryset = work_nodes


class BaseTransactionLineFormSet(forms.BaseFormSet):
    def add_fields(self, form, index):
        super().add_fields(form, index)
        form.fields["DELETE"].label = "تجاهل هذا السطر"

    def clean(self):
        if self.initial_form_count() != 0:
            raise forms.ValidationError("بيانات السطور غير صالحة.")


TransactionLineFormSet = forms.formset_factory(
    TransactionLineForm, formset=BaseTransactionLineFormSet, extra=0,
    min_num=1, validate_min=True, max_num=services.MAX_LINES, validate_max=True,
    absolute_max=services.MAX_LINES, can_delete=True,
)
