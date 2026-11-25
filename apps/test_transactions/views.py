import re

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from apps.access_control.policies import Action, projects_for
from apps.projects.models import Project

from . import services, storage
from .forms import TransactionForm, TransactionLineFormSet
from .models import TestTransaction


class TransactionRequestMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            response = self.get_response(request)
            match = request.resolver_match
            if match and match.func in (transaction_detail, download_attachment):
                response.headers["Cache-Control"] = "private, no-store"
            return response
        finally:
            handler = getattr(request, "private_pdf_upload", None)
            if handler is not None:
                handler.close()

    def process_view(self, request, view_func, view_args, view_kwargs):
        if view_func is create_transaction and request.method == "POST":
            if not request.user.is_authenticated:
                # The login_required wrapper redirects before the multipart body is read.
                return create_transaction(request, *view_args, **view_kwargs)
            services.require_project(
                request.user, view_kwargs["project_id"], Action.CREATE_TEST_DRAFT,
            )
            handler = storage.PrivatePDFUploadHandler(request)
            request.private_pdf_upload = handler
            request.upload_handlers = [handler]


@login_required
@require_safe
def projects(request, project_id=None):
    services.require_employee(request.user)
    employee_projects = projects_for(request.user).order_by("code")
    context = {"projects": employee_projects}
    if project_id is not None:
        project = services.require_project(request.user, project_id, Action.VIEW_TEST_TRANSACTION)
        transactions = services.visible_transactions(request.user, project_id).order_by("-created_at", "id")
        context.update(
            project=project,
            transactions=Paginator(transactions, 50).get_page(request.GET.get("page")),
            can_create=(project.status == Project.Status.ACTIVE and projects_for(
                request.user, Action.CREATE_TEST_DRAFT,
            ).filter(pk=project_id).exists()),
        )
    return render(request, "test_transactions/projects.html", context)


POST_FIELDS = {
    "csrfmiddlewaretoken", "title", "lines-TOTAL_FORMS", "lines-INITIAL_FORMS",
    "lines-MIN_NUM_FORMS", "lines-MAX_NUM_FORMS",
}
LINE_POST_FIELD = re.compile(r"lines-([0-9]{1,2})-(work_node|description|qty|unit_price|DELETE)")


def _valid_post_fields(post, files, line_forms):
    if set(files) != {"attachment"}:
        return False
    for key, values in post.lists():
        if len(values) != 1:
            return False
        if key in POST_FIELDS:
            continue
        line = LINE_POST_FIELD.fullmatch(key)
        if line is None or int(line[1]) >= line_forms.total_form_count():
            return False
    return True


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def create_transaction(request, project_id):
    try:
        project = services.require_project(request.user, project_id, Action.CREATE_TEST_DRAFT, for_write=True)
    except ValidationError as exc:
        return HttpResponseBadRequest(exc.messages[0])

    form_kwargs = {"work_nodes": services.eligible_work_nodes(project_id).order_by("code")}
    context = {"project": project, "max_lines": services.MAX_LINES}
    if request.method != "POST":
        context.update(form=TransactionForm(), line_forms=TransactionLineFormSet(prefix="lines", form_kwargs=form_kwargs))
        return render(request, "test_transactions/transaction_form.html", context)

    post = request.POST
    upload_error = request.private_pdf_upload.error
    if isinstance(upload_error, storage.PrivateStorageError):
        return HttpResponse("تعذّر حفظ المرفق الخاص.", status=503)
    if upload_error:
        return HttpResponseBadRequest(upload_error.messages[0])
    form = TransactionForm(post, request.FILES)
    line_forms = TransactionLineFormSet(post, prefix="lines", form_kwargs=form_kwargs)
    context.update(form=form, line_forms=line_forms)
    if not form.is_valid() or not line_forms.is_valid():
        return render(request, "test_transactions/transaction_form.html", context, status=400)
    if not _valid_post_fields(post, request.FILES, line_forms):
        return HttpResponseBadRequest("حقول الطلب غير صالحة.")

    lines = [
        {field: line[field] for field in services.LINE_FIELDS}
        for line in line_forms.cleaned_data if line and not line["DELETE"]
    ]
    try:
        draft = services.create_draft(
            actor=request.user, project_id=project_id, title=form.cleaned_data["title"],
            lines=lines, uploaded_file=form.cleaned_data["attachment"],
        )
    except ValidationError as exc:
        form.add_error(None, exc)
        return render(request, "test_transactions/transaction_form.html", context, status=400)
    except storage.PrivateStorageError:
        return HttpResponse("تعذّر حفظ المرفق الخاص.", status=503)
    return redirect("test_transactions:detail", project_id=project_id, transaction_id=draft.pk)


@login_required
@require_safe
def transaction_detail(request, project_id, transaction_id):
    draft = services.get_transaction(actor=request.user, project_id=project_id, transaction_id=transaction_id)
    lines = list(draft.lines.select_related("work_node").order_by("line_no"))
    can_approve = (
        draft.status == TestTransaction.Status.DRAFT
        and draft.project.status == Project.Status.ACTIVE
        and projects_for(request.user, Action.APPROVE_TEST_TRANSACTION).filter(pk=project_id).exists()
    )
    can_download = projects_for(request.user, Action.DOWNLOAD_TEST_ATTACHMENT).filter(pk=project_id).exists()
    return render(request, "test_transactions/transaction_detail.html", {
        "draft": draft, "lines": lines, "total": services.total_amount(lines),
        "attachments": draft.attachments.order_by("uploaded_at") if can_download else (),
        "can_approve": can_approve,
    })


@login_required
@require_POST
def approve_transaction(request, project_id, transaction_id):
    if set(request.POST) - {"csrfmiddlewaretoken"} or getattr(request, "unexpected_file_upload", False):
        return HttpResponseBadRequest("حقول الطلب غير صالحة.")
    try:
        services.approve_transaction(actor=request.user, project_id=project_id, transaction_id=transaction_id)
    except ValidationError as exc:
        return HttpResponseBadRequest(exc.messages[0])
    except services.TransitionConflict as exc:
        return HttpResponse(str(exc), status=409)
    return redirect("test_transactions:detail", project_id=project_id, transaction_id=transaction_id)


@require_safe
def download_attachment(request, project_id, transaction_id, attachment_id):
    attachment = services.get_attachment(
        actor=request.user, project_id=project_id, transaction_id=transaction_id, attachment_id=attachment_id,
    )
    try:
        file = storage.open_private_file(attachment.storage_key)
    except storage.PrivateStorageError:
        raise Http404 from None
    response = FileResponse(
        file, as_attachment=True, filename=attachment.original_name, content_type=storage.PDF_MEDIA_TYPE,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
