"""Enkripsi secret milik tenant (API key LLM) dengan Fernet (ADR 011).

AI_SECRET_KEY boleh berisi beberapa key dipisah koma: key pertama untuk enkripsi, semuanya dicoba
saat dekripsi, jadi rotasi cukup menambah key baru di depan lalu re-enkripsi data lama.
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import Settings


class SecretConfigError(RuntimeError):
    """AI_SECRET_KEY kosong atau formatnya salah."""


class SecretDecryptError(RuntimeError):
    """Ciphertext tidak bisa dibuka dengan key yang ada (key hilang atau data rusak)."""


def _fernet(settings: Settings) -> MultiFernet:
    raw = settings.ai_secret_key.get_secret_value() if settings.ai_secret_key else ""
    keys = [key.strip() for key in raw.split(",") if key.strip()]
    if not keys:
        raise SecretConfigError("AI_SECRET_KEY belum diisi.")
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except ValueError as exc:
        raise SecretConfigError("AI_SECRET_KEY bukan key Fernet yang valid.") from exc


def encrypt_secret(value: str, settings: Settings) -> str:
    return _fernet(settings).encrypt(value.encode()).decode()


def decrypt_secret(token: str, settings: Settings) -> str:
    try:
        return _fernet(settings).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretDecryptError("API key tersimpan tidak bisa dibuka.") from exc


def secret_hint(value: str) -> str:
    """4 karakter terakhir, untuk ditampilkan ke HR (misal ····a1b2)."""
    return value[-4:]
