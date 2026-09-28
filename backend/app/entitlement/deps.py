from collections.abc import Awaitable, Callable

from app.api.deps import CurrentUserDep, SettingsDep, TenantSessionDep
from app.core.errors import ForbiddenError
from app.entitlement.features import AI_FEATURES, AiFeature, Feature
from app.entitlement.service import ai_status


def require_feature(feature: Feature) -> Callable[..., Awaitable[None]]:
    """Dependency FastAPI: tolak request kalau fitur AI tidak aktif untuk tenant.

    Wajib dipasang di setiap endpoint fitur AI. Menyembunyikan tombol di UI saja tidak cukup.
    Fitur ERP selalu lolos.
    """

    async def dependency(
        user: CurrentUserDep, session: TenantSessionDep, settings: SettingsDep
    ) -> None:
        if feature not in AI_FEATURES:
            return
        status = await ai_status(session, user.tenant_id, settings)
        if AiFeature(feature.value) in status.active_features:
            return
        if status.status == "limit_reached":
            raise ForbiddenError(
                "Limit token AI bulan ini sudah habis. Aplikasi berjalan dalam mode ERP.",
                code="ai_limit_reached",
            )
        raise ForbiddenError(
            f"Fitur AI '{feature}' tidak aktif untuk perusahaan ini.", code="feature_not_enabled"
        )

    return dependency
