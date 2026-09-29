from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.reports.catalog import ColumnType

FilterOp = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "in",
    "not_in",
    "contains",
    "starts_with",
    "is_null",
    "is_not_null",
]
AggregateFunc = Literal["count", "count_distinct", "sum", "avg", "min", "max"]

MAX_COLUMNS = 30
MAX_FILTERS = 20
PREVIEW_LIMIT = 500


class ReportFilter(BaseModel):
    column: str = Field(max_length=64)
    op: FilterOp
    value: Any = Field(
        default=None,
        description=(
            "Satu nilai, list untuk in/not_in, [dari, sampai] untuk between, kosong untuk is_null."
        ),
    )


class ReportAggregate(BaseModel):
    func: AggregateFunc
    column: str | None = Field(default=None, max_length=64, description="Kosong = count baris.")


class ReportSort(BaseModel):
    column: str = Field(max_length=80, description="Kolom yang dipilih, atau key agregat.")
    direction: Literal["asc", "desc"] = "asc"


class ReportQuery(BaseModel):
    """Spesifikasi laporan (ADR 012). Dengan `aggregates`, baris dikelompokkan per `columns`."""

    dataset: str = Field(max_length=64)
    columns: list[str] = Field(default_factory=list, max_length=MAX_COLUMNS)
    filters: list[ReportFilter] = Field(default_factory=list, max_length=MAX_FILTERS)
    aggregates: list[ReportAggregate] = Field(default_factory=list, max_length=10)
    sort: list[ReportSort] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=PREVIEW_LIMIT, ge=1, le=PREVIEW_LIMIT)


class ReportColumnRead(BaseModel):
    key: str
    label: str
    type: ColumnType
    description: str
    enum_values: dict[str, str] | None = None
    operators: list[str]
    aggregates: list[str]


class ReportDatasetRead(BaseModel):
    key: str
    label: str
    description: str
    default_period_column: str | None
    columns: list[ReportColumnRead]


class ReportResultColumn(BaseModel):
    key: str
    label: str
    type: ColumnType


class ReportResult(BaseModel):
    columns: list[ReportResultColumn]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = Field(description="True kalau baris lebih banyak dari limit.")
    definition: list[str] = Field(
        description="Definisi yang dipakai (dataset, filter, periode, agregasi) untuk dicek user."
    )


class ReportTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    query: ReportQuery


class ReportTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    query: ReportQuery | None = None


class ReportTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    query: ReportQuery
    created_by_user_id: UUID
    updated_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


ExportFormat = Literal["csv", "xlsx"]
