from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    tenant_slug: str = Field(min_length=1, max_length=63)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    """Access token untuk header Authorization. Refresh token dikirim sebagai cookie httpOnly."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - skema OAuth2, bukan secret
    expires_in: int


class TenantSummary(BaseModel):
    id: UUID
    slug: str
    name: str


class MeResponse(BaseModel):
    id: UUID
    email: str
    roles: list[str]
    employee_id: UUID | None
    tenant: TenantSummary
