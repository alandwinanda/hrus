from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OrgUnitCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=200)
    parent_id: UUID | None = None
    manager_employee_id: UUID | None = None


class OrgUnitUpdate(BaseModel):
    """Hanya field yang dikirim yang diubah. Kirim null untuk mengosongkan parent/manager."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_id: UUID | None = None
    manager_employee_id: UUID | None = None
    is_active: bool | None = None


class OrgUnitRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    parent_id: UUID | None
    manager_employee_id: UUID | None
    is_active: bool
