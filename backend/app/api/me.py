from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUserDep, SettingsDep, TenantSessionDep
from app.schemas.auth import MeResponse
from app.services import me as me_service

router = APIRouter(tags=["me"])


@router.get("/me")
async def get_me(
    user: CurrentUserDep, session: TenantSessionDep, settings: SettingsDep
) -> MeResponse:
    """Profil user yang sedang login (MCP tool: get_my_profile), termasuk fitur AI yang aktif."""
    profile = await me_service.get_profile(session, user.user_id, settings)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "user_inactive", "message": "Akun tidak aktif. Silakan login ulang."},
        )
    return profile
