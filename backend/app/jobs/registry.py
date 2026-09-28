"""Definisi job (setara `job_definition` di SPEC). Disimpan di kode, bukan tabel, supaya
definisi dan logic-nya tidak bisa berbeda versi (ADR 009). Daftar job: `app/jobs/definitions.py`.
"""

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

MAX_CHANGES_PER_CHUNK = 1000  # detail perubahan per chunk, untuk review hasil dry-run


@dataclass(frozen=True, slots=True)
class ChunkContext:
    tenant_id: UUID
    run_id: UUID
    chunk_no: int
    params: Any  # instance params_model yang sudah divalidasi
    chunk: dict[str, Any]
    dry_run: bool
    actor_user_id: UUID | None


@dataclass(slots=True)
class ChunkResult:
    """Ringkasan (jumlah per jenis perubahan) dan detail perubahan satu chunk."""

    counts: Counter[str] = field(default_factory=Counter)
    changes: list[dict[str, Any]] = field(default_factory=list)

    def add(self, kind: str, **detail: Any) -> None:
        self.counts[kind] += 1
        if len(self.changes) < MAX_CHANGES_PER_CHUNK:
            self.changes.append({"kind": kind, **detail})

    def as_output(self) -> dict[str, Any]:
        return {"counts": dict(self.counts), "changes": self.changes}


PrepareFn = Callable[[AsyncSession, UUID, Any], Awaitable[Any]]
PlanFn = Callable[[AsyncSession, UUID, Any], Awaitable[list[dict[str, Any]]]]
RunChunkFn = Callable[[AsyncSession, ChunkContext], Awaitable[ChunkResult]]


@dataclass(frozen=True, slots=True)
class JobDefinition:
    """prepare: lengkapi dan validasi parameter saat run dibuat (misal as_of default hari ini),
    supaya semua chunk dan retry memakai nilai yang sama. plan: pecah data jadi chunk.
    run_chunk: kerjakan satu chunk, idempotent, di dalam transaksi yang disediakan runner."""

    code: str
    description: str
    params_model: type[BaseModel]
    prepare: PrepareFn
    plan: PlanFn
    run_chunk: RunChunkFn
    queue: str = "default"
    max_attempts: int = 3
    supports_dry_run: bool = True
    default_cron: str | None = None
