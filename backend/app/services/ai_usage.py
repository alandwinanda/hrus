"""Pencatatan pemakaian token LLM per tenant dan limit bulanan (ADR 011).

Tiap panggilan dicatat di ai_usage (append-only) dan counter ai_usage_monthly di transaksi yang
sama, jadi cek limit cukup membaca beberapa baris counter, bukan menjumlah seluruh log.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiUsage, AiUsageMonthly
from app.schemas.ai import AiUsageByFeature, AiUsageSummary
from app.services.tenant import tenant_zone

CONNECTION_TEST = "connection_test"


async def current_month(session: AsyncSession, tenant_id: UUID) -> date:
    today = datetime.now(await tenant_zone(session, tenant_id)).date()
    return today.replace(day=1)


async def record_usage(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    feature: str,
    provider: str,
    model: str,
    user_id: UUID | None,
    usage: dict[str, Any] | None,
    success: bool,
) -> None:
    usage = usage or {}
    tokens = {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "cached_input_tokens": int(usage.get("cached_input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }
    session.add(
        AiUsage(
            tenant_id=tenant_id,
            feature=feature,
            provider=provider,
            model=model,
            user_id=user_id,
            success=success,
            **tokens,
        )
    )
    month = await current_month(session, tenant_id)
    stmt = insert(AiUsageMonthly).values(
        tenant_id=tenant_id, month=month, feature=feature, calls=1, **tokens
    )
    excluded = stmt.excluded
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["tenant_id", "month", "feature"],
            set_={
                "calls": AiUsageMonthly.calls + 1,
                "input_tokens": AiUsageMonthly.input_tokens + excluded.input_tokens,
                "cached_input_tokens": AiUsageMonthly.cached_input_tokens
                + excluded.cached_input_tokens,
                "output_tokens": AiUsageMonthly.output_tokens + excluded.output_tokens,
                "updated_at": func.now(),
            },
        )
    )
    await session.flush()


async def month_tokens(session: AsyncSession, tenant_id: UUID, month: date) -> int:
    """Total token input + output bulan ini, dasar perhitungan limit."""
    total = await session.scalar(
        select(
            func.coalesce(func.sum(AiUsageMonthly.input_tokens + AiUsageMonthly.output_tokens), 0)
        ).where(AiUsageMonthly.tenant_id == tenant_id, AiUsageMonthly.month == month)
    )
    return int(total or 0)


async def usage_summary(
    session: AsyncSession, tenant_id: UUID, *, month: date | None, limit: int | None
) -> AiUsageSummary:
    month = month or await current_month(session, tenant_id)
    rows = await session.scalars(
        select(AiUsageMonthly)
        .where(AiUsageMonthly.tenant_id == tenant_id, AiUsageMonthly.month == month)
        .order_by(AiUsageMonthly.feature)
    )
    by_feature = [
        AiUsageByFeature(
            feature=row.feature,
            calls=row.calls,
            input_tokens=row.input_tokens,
            cached_input_tokens=row.cached_input_tokens,
            output_tokens=row.output_tokens,
        )
        for row in rows
    ]
    input_tokens = sum(f.input_tokens for f in by_feature)
    output_tokens = sum(f.output_tokens for f in by_feature)
    total = input_tokens + output_tokens
    return AiUsageSummary(
        month=month.strftime("%Y-%m"),
        calls=sum(f.calls for f in by_feature),
        input_tokens=input_tokens,
        cached_input_tokens=sum(f.cached_input_tokens for f in by_feature),
        output_tokens=output_tokens,
        total_tokens=total,
        monthly_token_limit=limit,
        limit_reached=limit is not None and total >= limit,
        by_feature=by_feature,
    )
