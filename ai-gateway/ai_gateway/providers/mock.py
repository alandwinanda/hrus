from uuid import uuid4

from ai_gateway.schemas import ChatMessage, ChatRequest, ChatResponse, Usage


def _estimate_tokens(text: str | None) -> int:
    return len(text or "") // 4


class MockProvider:
    """Provider palsu yang deterministik, untuk test dan dev tanpa API key."""

    name = "mock"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        last_user = next((m for m in reversed(request.messages) if m.role == "user"), None)
        content = f"[mock] {last_user.content if last_user else ''}".strip()
        return ChatResponse(
            id=f"mock-{uuid4().hex}",
            provider=self.name,
            model="mock",
            message=ChatMessage(role="assistant", content=content),
            finish_reason="stop",
            usage=Usage(
                input_tokens=sum(_estimate_tokens(m.content) for m in request.messages),
                output_tokens=_estimate_tokens(content),
            ),
        )
