from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr

Role = Literal["system", "user", "assistant", "tool"]


class ProviderCredentials(BaseModel):
    """Kredensial milik tenant (ADR 011), dikirim Core API per request. Tidak pernah di-log,
    tidak disimpan, dan tidak ikut diteruskan ke provider selain sebagai header Authorization."""

    provider: str = Field(min_length=1, max_length=32)
    base_url: str = Field(min_length=8, max_length=255)
    model: str = Field(min_length=1, max_length=100)
    api_key: SecretStr = Field(min_length=1, max_length=500)


class ChatMessage(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    """Format mengikuti OpenAI Chat Completions. Model dipilih gateway, bukan client."""

    messages: list[ChatMessage] = Field(min_length=1)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    credentials: ProviderCredentials | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


class ChatResponse(BaseModel):
    id: str
    provider: str
    model: str
    message: ChatMessage
    finish_reason: str | None = None
    usage: Usage
