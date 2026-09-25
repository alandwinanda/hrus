from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import Column, Date, Integer, MetaData, String, Table, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import (
    InvalidCursorError,
    apply_keyset,
    decode_cursor,
    encode_cursor,
    paginate,
)

metadata = MetaData()
item = Table(
    "tmp_page_item",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created", Date, nullable=False),
    Column("name", String, nullable=False),
)

ROWS = [
    {"id": 1, "created": date(2026, 1, 3), "name": "a"},
    {"id": 2, "created": date(2026, 1, 1), "name": "b"},
    {"id": 3, "created": date(2026, 1, 2), "name": "c"},
    {"id": 4, "created": date(2026, 1, 1), "name": "d"},
    {"id": 5, "created": date(2026, 1, 2), "name": "e"},
    {"id": 6, "created": date(2026, 1, 3), "name": "f"},
    {"id": 7, "created": date(2026, 1, 1), "name": "g"},
]


@pytest.fixture
async def seeded(session: AsyncSession) -> AsyncSession:
    await session.execute(
        text(
            "CREATE TEMP TABLE tmp_page_item "
            "(id int PRIMARY KEY, created date NOT NULL, name text NOT NULL) ON COMMIT DROP"
        )
    )
    await session.execute(insert(item), ROWS)
    return session


async def _collect(session: AsyncSession, limit: int, descending: bool) -> list[list[int]]:
    pages: list[list[int]] = []
    cursor: str | None = None
    while True:
        page = await paginate(
            session,
            select(item.c.id, item.c.name),
            [item.c.created, item.c.id],
            cursor,
            limit,
            descending=descending,
        )
        pages.append([row["id"] for row in page.items])
        if page.next_cursor is None:
            return pages
        cursor = page.next_cursor


async def test_paginate_ascending_visits_every_row_once(seeded: AsyncSession) -> None:
    pages = await _collect(seeded, limit=3, descending=False)

    assert pages == [[2, 4, 7], [3, 5, 1], [6]]


async def test_paginate_descending(seeded: AsyncSession) -> None:
    pages = await _collect(seeded, limit=4, descending=True)

    assert pages == [[6, 1, 5, 3], [7, 4, 2]]


async def test_paginate_items_hide_cursor_columns(seeded: AsyncSession) -> None:
    page = await paginate(seeded, select(item.c.id, item.c.name), [item.c.id], None, 2)

    assert page.items == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


async def test_paginate_single_column_returns_scalars(seeded: AsyncSession) -> None:
    page = await paginate(seeded, select(item.c.name), [item.c.id], None, 3)

    assert page.items == ["a", "b", "c"]


async def test_paginate_exact_fit_has_no_next_cursor(seeded: AsyncSession) -> None:
    page = await paginate(seeded, select(item.c.id), [item.c.id], None, len(ROWS))

    assert len(page.items) == len(ROWS)
    assert page.next_cursor is None


def test_query_uses_row_comparison_not_offset() -> None:
    cursor = encode_cursor([date(2026, 1, 1), 4])
    stmt = apply_keyset(select(item.c.id), [item.c.created, item.c.id], cursor, 10)
    sql = str(stmt.compile())

    assert "OFFSET" not in sql.upper()
    assert "(tmp_page_item.created, tmp_page_item.id) >" in sql


def test_cursor_round_trip_all_types() -> None:
    values = [
        True,
        42,
        1.5,
        "teks",
        datetime(2026, 9, 25, 8, 30, tzinfo=UTC),
        date(2026, 9, 25),
        uuid4(),
        Decimal("12.50"),
    ]

    assert decode_cursor(encode_cursor(values), len(values)) == values


@pytest.mark.parametrize("bad", ["bukan-cursor", "", "e30", "x" * 600])
def test_invalid_cursor_rejected(bad: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(bad, 1)


def test_cursor_size_mismatch_rejected() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(encode_cursor([1, 2]), 1)


@pytest.mark.parametrize("limit", [0, 201])
def test_limit_out_of_range_rejected(limit: int) -> None:
    with pytest.raises(ValueError):
        apply_keyset(select(item.c.id), [item.c.id], None, limit)
