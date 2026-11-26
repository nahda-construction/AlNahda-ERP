import json
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import PermissionDenied, RequestDataTooBig, ValidationError as DomainValidationError
from django.db.models import F
from django.http import Http404
from ninja import Header, NinjaAPI, Query, Status
from ninja.errors import HttpError, ValidationError
from ninja.openapi.docs import Swagger
from ninja.parser import Parser

from apps.access_control.policies import Action, projects_for
from apps.projects.models import WorkNode
from apps.test_transactions import services

from .access import MAX_JSON_BYTES, EmployeeSessionAuth, docs_permission_required, error_response
from .schemas import DraftInput, ErrorPayload, ProjectPage, TransactionDetail, WorkNodePage


class JSONParser(Parser):
    @staticmethod
    def unique_fields(fields):
        result = {}
        for name, value in fields:
            if name in result:
                raise ValueError("Duplicate JSON field")
            result[name] = value
        return result

    @staticmethod
    def reject_constant(value):
        raise ValueError("Nonstandard JSON number")

    def parse_body(self, request):
        body = request.body
        if len(body) > MAX_JSON_BYTES:
            raise ValueError("JSON body too large")
        data = json.loads(
            body.decode("utf-8"), parse_float=Decimal, parse_constant=self.reject_constant,
            object_pairs_hook=self.unique_fields,
        )
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data


api = NinjaAPI(
    title="ERP Internal API",
    version="1.0",
    urls_namespace="internal-api",
    docs_url="/docs/",
    openapi_url="/openapi.json",
    docs=Swagger(settings={"validatorUrl": None}),
    docs_decorator=docs_permission_required,
    auth=EmployeeSessionAuth(),
    parser=JSONParser(),
)

READ_ERRORS = {401: ErrorPayload, 403: ErrorPayload, 404: ErrorPayload, 422: ErrorPayload}
KNOWN_FIELDS = frozenset((
    "body", "payload", "lines", "title", "work_node_id", "description", "qty", "unit_price",
    "path", "project_id", "transaction_id", "query", "page", "page_size", "type", "node_type",
    "header", "Idempotency-Key", "idempotency_key",
))


@api.exception_handler(HttpError)
def handle_http_error(request, exc):
    status = 422 if exc.status_code == 400 else exc.status_code
    return error_response(status, csrf=(status == 403 and exc.message == "CSRF check Failed"))


@api.exception_handler(ValidationError)
def handle_parameter_errors(request, exc):
    fields = {}
    for error in exc.errors:
        path = ".".join(str(part) for part in error["loc"] if isinstance(part, int) or part in KNOWN_FIELDS)
        fields[path or "payload"] = ["هذه القيمة غير صالحة."]
    return error_response(422, field_errors=fields)


@api.exception_handler(DomainValidationError)
@api.exception_handler(RequestDataTooBig)
def handle_domain_validation(request, exc):
    return error_response(422)


@api.exception_handler(services.IdempotencyConflict)
@api.exception_handler(services.TransitionConflict)
def handle_conflict(request, exc):
    return error_response(409)


@api.exception_handler(PermissionDenied)
def handle_forbidden(request, exc):
    return error_response(403)


@api.exception_handler(Http404)
def handle_not_found(request, exc):
    return error_response(404)


def page_payload(rows, page, page_size):
    total = rows.count()
    start = (page - 1) * page_size
    items = list(rows[start:start + page_size]) if start < total else []
    return {"items": items, "page": page, "page_size": page_size, "total": total}


@api.get("/projects/", response={200: ProjectPage, **READ_ERRORS})
def projects(request, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    rows = projects_for(request.auth, Action.VIEW_PROJECT).order_by("code", "id")
    return page_payload(rows.values("id", "code", "name", "status"), page, page_size)


@api.get("/projects/{project_id}/work-nodes/", response={200: WorkNodePage, **READ_ERRORS})
def work_nodes(
    request, project_id: UUID, node_type: WorkNode.NodeType | None = Query(None, alias="type"),
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
):
    project = services.require_project(request.auth, project_id, Action.VIEW_WORK_NODES)
    rows = WorkNode.objects.filter(project=project).order_by("code", "id")
    if node_type is not None:
        rows = rows.filter(node_type=node_type)
    return page_payload(rows.values("id", "parent_id", "code", "name", type=F("node_type")), page, page_size)


@api.get("/projects/{project_id}/test-transactions/{transaction_id}/", response={200: TransactionDetail, **READ_ERRORS})
def transaction_detail(request, project_id: UUID, transaction_id: UUID):
    draft = services.get_transaction(actor=request.auth, project_id=project_id, transaction_id=transaction_id)
    attachment_ids = ()
    if projects_for(request.auth, Action.DOWNLOAD_TEST_ATTACHMENT).filter(pk=project_id).exists():
        attachment_ids = draft.attachments.order_by("uploaded_at", "id").values_list("id", flat=True)
    return services.transaction_detail_payload(draft, list(draft.lines.order_by("line_no")), attachment_ids)


@api.post("/projects/{project_id}/test-transactions/", response={201: TransactionDetail, 409: ErrorPayload, **READ_ERRORS})
def create_transaction(request, project_id: UUID, payload: DraftInput, idempotency_key: UUID = Header(..., alias="Idempotency-Key")):
    lines = [
        {"work_node": line.work_node_id, "description": line.description, "qty": line.qty, "unit_price": line.unit_price}
        for line in payload.lines
    ]
    receipt = services.create_json_draft_once(
        actor=request.auth, project_id=project_id, key=idempotency_key, title=payload.title, lines=lines,
    )
    return Status(receipt.response_status, receipt.response_body)
