from pydantic import BaseModel


class Page[T](BaseModel):
    """Response standar endpoint list (keyset pagination)."""

    items: list[T]
    next_cursor: str | None = None
