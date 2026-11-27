from typing import Annotated
from uuid import UUID

from ninja import Schema
from pydantic import ConfigDict, Field, StrictStr

from apps.test_transactions.services import MAX_LINES


class ProjectItem(Schema):
    id: UUID
    code: str
    name: str
    status: str


class WorkNodeItem(Schema):
    id: UUID
    parent_id: UUID | None
    type: str
    code: str
    name: str


class Page(Schema):
    page: int
    page_size: int
    total: int


class ProjectPage(Page):
    items: list[ProjectItem]


class WorkNodePage(Page):
    items: list[WorkNodeItem]


class DraftLineInput(Schema):
    model_config = ConfigDict(extra="forbid")

    work_node_id: UUID
    description: Annotated[StrictStr, Field(max_length=240)]
    qty: Annotated[StrictStr, Field(min_length=1, max_length=32)]
    unit_price: Annotated[StrictStr, Field(min_length=1, max_length=32)]


class DraftInput(Schema):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[StrictStr, Field(min_length=1, max_length=160)]
    lines: Annotated[list[DraftLineInput], Field(min_length=1, max_length=MAX_LINES)]


class TransactionLine(Schema):
    line_no: int
    work_node_id: UUID
    description: str
    qty: str
    unit_price: str
    amount: str


class TransactionDetail(Schema):
    id: UUID
    reference: str
    status: str
    title: str
    lines: list[TransactionLine]
    total: str
    created_at: Annotated[str, Field(json_schema_extra={"format": "date-time"})]
    attachment_ids: list[UUID]


class ErrorPayload(Schema):
    code: str
    message: str
    correlation_id: UUID
    field_errors: dict[str, list[str]] | None = None
