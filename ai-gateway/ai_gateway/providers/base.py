from typing import Protocol

from ai_gateway.schemas import ChatRequest, ChatResponse


class ProviderError(Exception):
    """Provider gagal, timeout, atau responsnya tidak valid. Client kembali ke mode ERP."""


class ProviderNotConfiguredError(Exception):
    """Provider dipilih tapi konfigurasinya belum lengkap (misal API key kosong)."""


class ChatProvider(Protocol):
    name: str

    async def chat(self, request: ChatRequest) -> ChatResponse: ...
