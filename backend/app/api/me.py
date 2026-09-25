from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUserDep, TenantSessionDep
from app.schemas.auth import MeResponse
from app.services import me as me_service

router = APIRouter(tags=["me"])


@router.get("/me")
async def get_me(user: CurrentUserDep, session: TenantSessionDep) -> MeResponse:
    """Profil user yang sedang login (MCP tool: get_my_profile)."""
    profile = await me_service.get_profile(session, user.user_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "user_inactive", "message": "Akun tidak aktif. Silakan login ulang."},
        )
    return profile
