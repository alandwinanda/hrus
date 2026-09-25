from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.core.config import Settings, get_settings
from app.entitlement.features import Feature
from app.entitlement.service import is_feature_enabled


def require_feature(feature: Feature) -> Callable[..., Awaitable[None]]:
    """Dependency FastAPI: tolak request kalau fitur tidak aktif untuk tenant.

    Wajib dipasang di backend untuk setiap endpoint fitur berbayar.
    Menyembunyikan tombol di UI saja tidak cukup.
    """

    async def dependency(settings: Annotated[Settings, Depends(get_settings)]) -> None:
        if not is_feature_enabled(feature, settings):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "feature_not_enabled",
                    "message": f"Fitur '{feature}' tidak aktif untuk paket ini.",
                },
            )

    return dependency
