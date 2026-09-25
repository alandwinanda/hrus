"""Keyset pagination. OFFSET dilarang untuk list besar karena makin lambat di halaman belakang.

Aturan pemakaian:
- Kolom sort wajib NOT NULL, dan kombinasinya wajib unik (akhiri dengan primary key).
- Urutan kolom sort sebaiknya sama dengan composite index, misal (tenant_id, start_date, id).
- Semua kolom sort memakai arah yang sama (asc semua atau desc semua).
"""

import base64
import binascii
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Row, Select, literal, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_CURSOR_LENGTH = 512
_CURSOR_LABEL = "_cursor_"

type CursorValue = bool | int | float | str | datetime | date | UUID | Decimal


class InvalidCursorError(ValueError):
    """Cursor dari client tidak valid. API mengembalikan 400."""


def _encode_value(value: CursorValue) -> list[Any]:
    # Urutan cek penting: bool subclass dari int, datetime subclass dari date.
    if isinstance(value, bool):
        return ["b", value]
    if isinstance(value, int):
        return ["i", value]
    if isinstance(value, float):
        return ["f", value]
    if isinstance(value, str):
        return ["s", value]
    if isinstance(value, datetime):
        return ["dt", value.isoformat()]
    if isinstance(value, date):
        return ["d", value.isoformat()]
    if isinstance(value, UUID):
        return ["u", str(value)]
    if isinstance(value, Decimal):
        return ["n", str(value)]
    raise ValueError(f"Tipe {type(value).__name__} tidak didukung sebagai kolom cursor")


def _decode_value(tag: str, raw: Any) -> CursorValue:
    decoders = {
        "b": bool,
        "i": int,
        "f": float,
        "s": str,
        "dt": datetime.fromisoformat,
        "d": date.fromisoformat,
        "u": UUID,
        "n": Decimal,
    }
    return decoders[tag](raw)


def encode_cursor(values: Sequence[CursorValue]) -> str:
    payload = json.dumps([_encode_value(v) for v in values], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, expected_size: int) -> list[CursorValue]:
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise InvalidCursorError("cursor terlalu panjang")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        items = json.loads(base64.urlsafe_b64decode(padded.encode()))
        values = [_decode_value(tag, raw) for tag, raw in items]
    except (binascii.Error, ValueError, TypeError, KeyError) as exc:
        raise InvalidCursorError("cursor tidak valid") from exc
    if len(values) != expected_size:
        raise InvalidCursorError("cursor tidak cocok dengan urutan sort")
    return values


def apply_keyset(
    stmt: Select[Any],
    sort_columns: Sequence[ColumnElement[Any]],
    cursor: str | None,
    limit: int,
    *,
    descending: bool = False,
) -> Select[Any]:
    """Tambahkan WHERE (row comparison), ORDER BY, dan LIMIT limit+1 ke query."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit harus 1 sampai {MAX_LIMIT}")
    if not sort_columns:
        raise ValueError("sort_columns wajib diisi")

    if cursor is not None:
        values = decode_cursor(cursor, len(sort_columns))
        keys = tuple_(*sort_columns)
        last = tuple_(*(literal(v, c.type) for v, c in zip(values, sort_columns, strict=True)))
        stmt = stmt.where(keys < last if descending else keys > last)

    order = [c.desc() if descending else c.asc() for c in sort_columns]
    return stmt.order_by(*order).limit(limit + 1)


@dataclass(frozen=True, slots=True)
class KeysetPage[T]:
    items: list[T]
    next_cursor: str | None


def _strip_cursor_columns(row: Row[Any], size: int) -> Any:
    values = tuple(row)[:-size]
    if len(values) == 1:
        return values[0]
    return {
        key: value
        for key, value in row._mapping.items()
        if not (isinstance(key, str) and key.startswith(_CURSOR_LABEL))
    }


async def paginate(
    session: AsyncSession,
    stmt: Select[Any],
    sort_columns: Sequence[ColumnElement[Any]],
    cursor: str | None,
    limit: int = DEFAULT_LIMIT,
    *,
    descending: bool = False,
) -> KeysetPage[Any]:
    """Jalankan query dengan keyset pagination.

    Item berisi object/nilai tunggal kalau query memilih satu entity atau kolom,
    atau dict kolom kalau query memilih beberapa kolom.
    """
    size = len(sort_columns)
    labeled = [c.label(f"{_CURSOR_LABEL}{i}") for i, c in enumerate(sort_columns)]
    query = apply_keyset(
        stmt.add_columns(*labeled), sort_columns, cursor, limit, descending=descending
    )
    rows = (await session.execute(query)).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(tuple(rows[-1])[-size:]) if has_more else None
    return KeysetPage(items=[_strip_cursor_columns(r, size) for r in rows], next_cursor=next_cursor)
