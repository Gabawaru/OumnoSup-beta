"""Shared FastAPI dependencies: pagination, sessions, admin authentication."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.core.database import get_session
from src.core.exceptions import AuthenticationError

__all__ = ["Pagination", "SessionDep", "get_pagination", "require_admin"]

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@dataclass(frozen=True, slots=True)
class Pagination:
    """A validated page request.

    Attributes:
        page: 1-based page number.
        page_size: Rows per page, already clamped to the configured maximum.
    """

    page: int
    page_size: int

    @property
    def offset(self) -> int:
        """Row offset for the SQL query."""
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """Row limit for the SQL query."""
        return self.page_size


def get_pagination(
    page: Annotated[int, Query(ge=1, description="1-based page number")] = 1,
    page_size: Annotated[
        int | None, Query(ge=1, description="Rows per page")
    ] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> Pagination:
    """Build a page request from query parameters.

    ``page_size`` is clamped rather than rejected: a client asking for 5000 rows
    gets the maximum with a full response, instead of an error it has to learn
    to handle.

    Args:
        page: 1-based page number.
        page_size: Requested rows per page, defaulting to the configured value.
        settings: Application settings.

    Returns:
        The validated pagination window.
    """
    settings = settings or get_settings()
    size = page_size or settings.api_default_page_size
    return Pagination(page=page, page_size=min(size, settings.api_max_page_size))


PaginationDep = Annotated[Pagination, Depends(get_pagination)]


async def require_admin(
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> None:
    """Reject a request that does not carry the admin token.

    Compared with :func:`hmac.compare_digest` so the check does not leak the
    token's length or content through response timing.

    Args:
        x_admin_token: Value of the ``X-Admin-Token`` header.
        settings: Application settings.

    Raises:
        AuthenticationError: If the header is missing or does not match.
    """
    settings = settings or get_settings()
    expected = settings.admin_token.get_secret_value()
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise AuthenticationError("a valid X-Admin-Token header is required")


AdminDep = Annotated[None, Depends(require_admin)]
