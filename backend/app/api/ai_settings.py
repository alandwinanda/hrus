from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUserDep, SettingsDep, TenantSessionDep, require_roles
from app.models import Role
from app.schemas.ai import AiConnectionTestResult, AiSettingsRead, AiSettingsUpdate, AiUsageSummary
from app.services import ai_settings as service
from app.services.ai_gateway import AiGateway, get_ai_gateway

router = APIRouter(prefix="/settings/ai", tags=["settings-ai"])

HrAdmin = Annotated[object, Depends(require_roles(Role.HR_ADMIN))]
GatewayDep = Annotated[AiGateway, Depends(get_ai_gateway)]


@router.get("")
async def get_ai_settings(
    user: CurrentUserDep, session: TenantSessionDep, settings: SettingsDep, _: HrAdmin
) -> AiSettingsRead:
    """Provider, model, status API key (tanpa key-nya), fitur AI, limit, dan status kesiapan."""
    return await service.get_ai_settings(session, user.tenant_id, settings)


@router.patch("")
async def update_ai_settings(
    body: AiSettingsUpdate,
    user: CurrentUserDep,
    session: TenantSessionDep,
    settings: SettingsDep,
    _: HrAdmin,
) -> AiSettingsRead:
    """Ganti key, provider, atau model mewajibkan tes koneksi ulang. Ganti provider juga
    mewajibkan persetujuan ulang. Fitur AI hanya bisa diaktifkan setelah keduanya beres."""
    return await service.update_ai_settings(session, user, body, settings)


@router.post("/test")
async def test_ai_connection(
    user: CurrentUserDep,
    session: TenantSessionDep,
    settings: SettingsDep,
    gateway: GatewayDep,
    _: HrAdmin,
) -> AiConnectionTestResult:
    """Coba API key lewat ai-gateway (1 token). Gagal tidak dianggap error: lihat `success`."""
    return await service.test_connection(session, user, settings, gateway)


@router.get("/usage")
async def get_ai_usage(
    user: CurrentUserDep,
    session: TenantSessionDep,
    _: HrAdmin,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
) -> AiUsageSummary:
    """Pemakaian token per fitur untuk satu bulan (default bulan berjalan)."""
    return await service.get_usage(session, user.tenant_id, year=year, month=month)
