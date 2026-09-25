from typing import Literal

from pydantic import BaseModel

CheckStatus = Literal["ok", "error"]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


class ReadinessChecks(BaseModel):
    database: CheckStatus
    redis: CheckStatus


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks
