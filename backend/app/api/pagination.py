from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query

from app.core.pagination import DEFAULT_LIMIT, MAX_CURSOR_LENGTH, MAX_LIMIT


@dataclass(frozen=True, slots=True)
class PageParams:
    cursor: str | None
    limit: int


def _page_params(
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> PageParams:
    return PageParams(cursor=cursor, limit=limit)


PageParamsDep = Annotated[PageParams, Depends(_page_params)]
