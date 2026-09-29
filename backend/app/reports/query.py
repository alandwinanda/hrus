"""Compiler spesifikasi laporan ke SQLAlchemy Core (ADR 012).

Nama kolom, operator, dan fungsi agregat hanya boleh dari katalog. Semua nilai filter jadi bind
parameter setelah divalidasi sesuai tipe kolom. Filter tenant_id selalu ditambahkan (RLS lapis
kedua), dan dataset transaksi tanpa filter periode otomatis dibatasi tahun berjalan.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import StrictBool, TypeAdapter, ValidationError, constr
from sqlalchemy import (
    Boolean,
    ColumnElement,
    Date,
    DateTime,
    Numeric,
    Select,
    String,
    column,
    distinct,
    func,
    select,
    table,
)
from sqlalchemy.types import TypeEngine

from app.core.errors import RuleViolationError
from app.reports.catalog import ColumnDef, ColumnType, Dataset, get_dataset
from app.schemas.reports import ReportFilter, ReportQuery, ReportResultColumn

T = ColumnType
MAX_IN_VALUES = 100

COMPARE = {"eq", "ne", "gt", "gte", "lt", "lte"}
NULL_OPS = {"is_null", "is_not_null"}
OPS_BY_TYPE: dict[ColumnType, tuple[str, ...]] = {
    T.TEXT: ("eq", "ne", "in", "not_in", "contains", "starts_with", "is_null", "is_not_null"),
    T.ENUM: ("eq", "ne", "in", "not_in", "is_null", "is_not_null"),
    T.NUMBER: (
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "in",
        "not_in",
        "is_null",
        "is_not_null",
    ),
    T.DATE: ("eq", "ne", "gt", "gte", "lt", "lte", "between", "is_null", "is_not_null"),
    T.DATETIME: ("gt", "gte", "lt", "lte", "between", "is_null", "is_not_null"),
    T.BOOLEAN: ("eq", "is_null", "is_not_null"),
}
AGGREGATES_BY_TYPE: dict[ColumnType, tuple[str, ...]] = {
    T.TEXT: ("count", "count_distinct"),
    T.ENUM: ("count", "count_distinct"),
    T.BOOLEAN: ("count", "count_distinct"),
    T.NUMBER: ("count", "count_distinct", "sum", "avg", "min", "max"),
    T.DATE: ("count", "count_distinct", "min", "max"),
    T.DATETIME: ("count", "count_distinct", "min", "max"),
}
SQL_TYPES: dict[ColumnType, TypeEngine[Any]] = {
    T.TEXT: String(),
    T.ENUM: String(),
    T.NUMBER: Numeric(),
    T.DATE: Date(),
    T.DATETIME: DateTime(timezone=True),
    T.BOOLEAN: Boolean(),
}
VALUE_ADAPTERS: dict[ColumnType, TypeAdapter[Any]] = {
    T.TEXT: TypeAdapter(constr(min_length=1, max_length=200)),
    T.ENUM: TypeAdapter(constr(min_length=1, max_length=64)),
    T.NUMBER: TypeAdapter(Decimal),
    T.DATE: TypeAdapter(date),
    T.DATETIME: TypeAdapter(datetime),
    T.BOOLEAN: TypeAdapter(StrictBool),
}
OP_LABELS = {
    "eq": "=",
    "ne": "≠",
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "between": "antara",
    "in": "salah satu dari",
    "not_in": "bukan salah satu dari",
    "contains": "mengandung",
    "starts_with": "diawali",
    "is_null": "kosong",
    "is_not_null": "tidak kosong",
}
AGGREGATE_LABELS = {
    "count": "Jumlah",
    "count_distinct": "Jumlah unik",
    "sum": "Total",
    "avg": "Rata-rata",
    "min": "Minimum",
    "max": "Maksimum",
}


def _invalid(message: str, code: str) -> RuleViolationError:
    return RuleViolationError(message, code=code)


@dataclass(slots=True)
class CompiledReport:
    dataset: Dataset
    stmt: Select[Any]
    columns: list[ReportResultColumn]
    # Kolom katalog per posisi hasil (None untuk agregat), untuk label enum saat export.
    sources: list[ColumnDef | None]
    definition: list[str] = field(default_factory=list)


def _coerce(col: ColumnDef, value: Any) -> Any:
    if value is None:
        raise _invalid(f"Nilai filter '{col.label}' wajib diisi.", "report_invalid_value")
    try:
        coerced = VALUE_ADAPTERS[col.type].validate_python(value)
    except ValidationError as exc:
        raise _invalid(
            f"Nilai '{value}' tidak cocok untuk kolom {col.label} ({col.type}).",
            "report_invalid_value",
        ) from exc
    if col.type == T.ENUM and coerced not in dict(col.enum_values):
        raise _invalid(
            f"'{coerced}' bukan pilihan yang valid untuk {col.label}.", "report_invalid_value"
        )
    return coerced


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _display(col: ColumnDef, value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_display(col, v) for v in value)
    if col.type == T.ENUM:
        return col.enum_label(value)
    if isinstance(value, bool):
        return "Ya" if value else "Tidak"
    return str(value)


def _filter_expr(
    target: ColumnElement[Any], col: ColumnDef, flt: ReportFilter
) -> tuple[ColumnElement[bool], str]:
    op = flt.op
    if op not in OPS_BY_TYPE[col.type]:
        raise _invalid(
            f"Operator '{op}' tidak bisa dipakai untuk kolom {col.label} ({col.type}).",
            "report_invalid_operator",
        )
    if op in NULL_OPS:
        expr = target.is_(None) if op == "is_null" else target.is_not(None)
        return expr, f"{col.label} {OP_LABELS[op]}"

    if op in ("in", "not_in", "between"):
        values = flt.value
        if not isinstance(values, list) or not values:
            raise _invalid(
                f"Filter '{op}' untuk {col.label} butuh daftar nilai.", "report_invalid_value"
            )
        if op == "between" and len(values) != 2:
            raise _invalid(
                f"Filter 'between' untuk {col.label} butuh 2 nilai.", "report_invalid_value"
            )
        if len(values) > MAX_IN_VALUES:
            raise _invalid(f"Maksimal {MAX_IN_VALUES} nilai per filter.", "report_invalid_value")
        coerced = [_coerce(col, v) for v in values]
        if op == "between":
            expr = target.between(coerced[0], coerced[1])
            text = f"{col.label} antara {_display(col, coerced[0])} dan {_display(col, coerced[1])}"
            return expr, text
        expr = target.in_(coerced) if op == "in" else target.not_in(coerced)
        return expr, f"{col.label} {OP_LABELS[op]} {_display(col, coerced)}"

    value = _coerce(col, flt.value)
    if op == "contains":
        expr = target.ilike(f"%{_escape_like(value)}%", escape="\\")
    elif op == "starts_with":
        expr = target.ilike(f"{_escape_like(value)}%", escape="\\")
    else:
        expr = {
            "eq": target == value,
            "ne": target != value,
            "gt": target > value,
            "gte": target >= value,
            "lt": target < value,
            "lte": target <= value,
        }[op]
    return expr, f"{col.label} {OP_LABELS[op]} {_display(col, value)}"


def compile_query(spec: ReportQuery, tenant_id: UUID, today: date) -> CompiledReport:
    dataset = get_dataset(spec.dataset)
    if not spec.columns and not spec.aggregates:
        raise _invalid("Pilih minimal satu kolom atau agregat.", "report_no_columns")
    if len(set(spec.columns)) != len(spec.columns):
        raise _invalid("Kolom yang sama dipilih lebih dari sekali.", "report_duplicate_column")

    view = table(
        dataset.view,
        column("tenant_id"),
        *(column(c.key, SQL_TYPES[c.type]) for c in dataset.columns),
    )
    dims = [dataset.column(key) for key in spec.columns]
    selected: dict[str, ColumnElement[Any]] = {c.key: view.c[c.key] for c in dims}
    result_columns = [ReportResultColumn(key=c.key, label=c.label, type=c.type) for c in dims]
    sources: list[ColumnDef | None] = list(dims)
    definition = [f"Dataset: {dataset.label}"]

    stmt = select().select_from(view).where(view.c.tenant_id == tenant_id)
    filtered_columns = set()
    for flt in spec.filters:
        col = dataset.column(flt.column)
        expr, text = _filter_expr(view.c[col.key], col, flt)
        stmt = stmt.where(expr)
        filtered_columns.add(col.key)
        definition.append(f"Filter: {text}")

    period = dataset.default_period_column
    if period is not None and period not in filtered_columns:
        start, end = date(today.year, 1, 1), date(today.year, 12, 31)
        stmt = stmt.where(view.c[period].between(start, end))
        label = dataset.column(period).label
        definition.append(f"Periode default: {label} {start} s.d. {end} (tahun berjalan)")

    for agg in spec.aggregates:
        if agg.column is None:
            if agg.func != "count":
                raise _invalid(f"Agregat '{agg.func}' butuh kolom.", "report_invalid_aggregate")
            key, label, expr, kind = "count", "Jumlah baris", func.count(), T.NUMBER
        else:
            col = dataset.column(agg.column)
            if agg.func not in AGGREGATES_BY_TYPE[col.type]:
                raise _invalid(
                    f"Agregat '{agg.func}' tidak bisa dipakai untuk kolom {col.label}.",
                    "report_invalid_aggregate",
                )
            target = view.c[col.key]
            expr = {
                "count": func.count(target),
                "count_distinct": func.count(distinct(target)),
                "sum": func.sum(target),
                "avg": func.round(func.avg(target), 2),
                "min": func.min(target),
                "max": func.max(target),
            }[agg.func]
            key = f"{agg.func}_{col.key}"
            label = f"{AGGREGATE_LABELS[agg.func]} {col.label.lower()}"
            kind = col.type if agg.func in ("min", "max") else T.NUMBER
        if key in selected:
            raise _invalid(f"Agregat '{key}' dipilih lebih dari sekali.", "report_duplicate_column")
        selected[key] = expr
        result_columns.append(ReportResultColumn(key=key, label=label, type=kind))
        sources.append(None)
        definition.append(f"Agregat: {label}")

    stmt = stmt.with_only_columns(*(expr.label(key) for key, expr in selected.items()))
    if spec.aggregates and dims:
        stmt = stmt.group_by(*(view.c[c.key] for c in dims))
        definition.append("Dikelompokkan per: " + ", ".join(c.label for c in dims))

    order = []
    for sort in spec.sort:
        if sort.column not in selected:
            raise _invalid(
                f"Urutan '{sort.column}' harus salah satu kolom atau agregat yang dipilih.",
                "report_invalid_sort",
            )
        expr = selected[sort.column]
        order.append(expr.desc() if sort.direction == "desc" else expr.asc())
    if not order:
        order = [next(iter(selected.values())).asc()]
    stmt = stmt.order_by(*(o.nulls_last() for o in order))

    return CompiledReport(dataset, stmt, result_columns, sources, definition)


def to_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value
