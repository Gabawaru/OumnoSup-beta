"""Application configuration.

Every tunable in OumnoSup is declared here and nowhere else. Values are read
from environment variables (or a local ``.env`` file), validated by Pydantic at
startup, and consumed through :func:`get_settings`.

No secret is ever hard-coded: credentials are typed as :class:`~pydantic.SecretStr`
so that they are redacted from ``repr()`` output, tracebacks and structured logs.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Final

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    """Deployment environment the process is running in."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


#: Values shipped in ``.env.example``. They are convenient for local development
#: and unacceptable in production, so the settings model refuses to start with
#: them once ``ENVIRONMENT=production``.
_PLACEHOLDER_SECRETS: Final[frozenset[str]] = frozenset(
    {
        "",
        "change-me",
        "change-me-in-production",
        "change-me-too",
        "changeme",
        "secret",
    }
)

_VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
)

#: The database layer is fully asynchronous, so a synchronous driver would fail
#: later with a confusing error inside ``create_async_engine``. Catch it early.
_ASYNC_DB_SCHEME: Final[str] = "postgresql+asyncpg://"


class Settings(BaseSettings):
    """Runtime configuration for every OumnoSup process.

    Attributes are populated from environment variables of the same name,
    matched case-insensitively (``database_url`` reads ``DATABASE_URL``).

    Note:
        Validators raise :class:`ValueError`, which Pydantic collects into a
        ``ValidationError``. This is Pydantic's required contract for validators
        and is the one place the project's "no bare exceptions" rule defers to a
        framework convention.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Environment
    # ------------------------------------------------------------------
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    database_url: str = (
        "postgresql+asyncpg://oumnosup:oumnosup@localhost:5432/oumnosup"
    )
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout: int = Field(default=30, ge=1)

    # ------------------------------------------------------------------
    # Redis (request queue, response cache, distributed rate limiting)
    # ------------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # ------------------------------------------------------------------
    # Scraping
    # ------------------------------------------------------------------
    scrape_delay_seconds: float = Field(default=1.0, ge=0.0)
    scrape_max_retries: int = Field(default=3, ge=0)
    scrape_timeout: int = Field(default=30, ge=1)

    #: Backoff delays in seconds between successive retries. Consumed positionally:
    #: the first retry waits ``[0]``, the second ``[1]``, and so on; once exhausted
    #: the last value repeats.
    scrape_retry_backoff: list[int] = Field(default=[5, 30, 120])

    #: When a platform's ``robots.txt`` declares a ``Crawl-delay``, honour it if it
    #: is *slower* than ``scrape_delay_seconds``. Parcoursup declares
    #: ``Crawl-delay: 10``, so the project-wide 1 req/s default would breach it.
    scrape_respect_robots_crawl_delay: bool = True

    #: Sent on every outbound request and matched against ``robots.txt`` groups.
    #: A scraper that identifies itself can be contacted, rate-limited or excluded
    #: by a platform operator instead of silently blocked.
    scrape_user_agent: str = "OumnoSupBot/0.1 (+https://oumnosup.com/bot)"

    #: Comma-separated proxy URLs. Kept as a raw string because a plain empty
    #: value is not valid JSON; use :attr:`proxies` for the parsed list.
    proxy_list: str = ""

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_rate_limit_per_minute: int = Field(default=100, ge=1)

    #: Must be provided as a JSON array, e.g.
    #: ``API_CORS_ORIGINS=["http://localhost:3000","https://oumnosup.com"]``.
    api_cors_origins: list[str] = Field(
        default=["http://localhost:3000", "https://oumnosup.com"]
    )

    api_default_page_size: int = Field(default=20, ge=1)
    api_max_page_size: int = Field(default=100, ge=1)
    api_cache_ttl_seconds: int = Field(default=300, ge=0)

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------
    secret_key: SecretStr = SecretStr("change-me-in-production")
    admin_token: SecretStr = SecretStr("change-me-too")
    anthropic_api_key: SecretStr | None = None

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """Normalise the log level to upper case and reject unknown levels.

        Args:
            value: Raw log level read from the environment.

        Returns:
            The upper-cased level name.

        Raises:
            ValueError: If the level is not one understood by Loguru.
        """
        normalised = value.strip().upper()
        if normalised not in _VALID_LOG_LEVELS:
            expected = ", ".join(sorted(_VALID_LOG_LEVELS))
            raise ValueError(f"LOG_LEVEL must be one of: {expected} (got {value!r})")
        return normalised

    @field_validator("database_url")
    @classmethod
    def _validate_async_database_url(cls, value: str) -> str:
        """Normalise the database URL onto the async driver.

        Every managed PostgreSQL provider — Render, Railway, Neon, Supabase,
        Heroku — hands out a ``postgres://`` or ``postgresql://`` URL. Rejecting
        those would make the application fail to boot on all of them, so the
        bare PostgreSQL schemes are rewritten onto ``postgresql+asyncpg``
        instead. Any other driver is still refused, because a synchronous or
        non-PostgreSQL URL fails later inside ``create_async_engine`` with a far
        less useful error.

        Args:
            value: Raw database URL read from the environment.

        Returns:
            The URL, rewritten onto the async driver when necessary.

        Raises:
            ValueError: If the URL names a driver this application cannot use.
        """
        url = value.strip()
        if url.startswith(_ASYNC_DB_SCHEME):
            return url
        for bare in ("postgresql://", "postgres://"):
            if url.startswith(bare):
                return _ASYNC_DB_SCHEME + url[len(bare):]
        raise ValueError(
            f"DATABASE_URL must be a PostgreSQL URL — {_ASYNC_DB_SCHEME!r}, "
            f"'postgresql://' or 'postgres://' (got {value!r})"
        )

    @field_validator("api_max_page_size")
    @classmethod
    def _validate_page_sizes(cls, value: int) -> int:
        """Reject a maximum page size below the floor the API advertises.

        Args:
            value: Configured maximum page size.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If the maximum is below 1.
        """
        if value < 1:
            raise ValueError("API_MAX_PAGE_SIZE must be at least 1")
        return value

    @model_validator(mode="after")
    def _validate_consistency(self) -> Settings:
        """Cross-field checks that cannot be expressed on a single field.

        Refuses to boot a production process that still carries the example
        secrets, and keeps the pagination bounds coherent.

        Returns:
            The validated settings instance.

        Raises:
            ValueError: If a production secret is still a placeholder, or if the
                default page size exceeds the maximum.
        """
        if self.api_default_page_size > self.api_max_page_size:
            raise ValueError(
                f"API_DEFAULT_PAGE_SIZE ({self.api_default_page_size}) cannot exceed "
                f"API_MAX_PAGE_SIZE ({self.api_max_page_size})"
            )

        if not self.scrape_retry_backoff:
            raise ValueError("SCRAPE_RETRY_BACKOFF must contain at least one delay")
        if any(delay < 0 for delay in self.scrape_retry_backoff):
            raise ValueError("SCRAPE_RETRY_BACKOFF delays must be non-negative")

        if self.environment is Environment.PRODUCTION:
            placeholders = [
                name
                for name, secret in (
                    ("SECRET_KEY", self.secret_key),
                    ("ADMIN_TOKEN", self.admin_token),
                )
                if secret.get_secret_value().strip().lower() in _PLACEHOLDER_SECRETS
            ]
            if placeholders:
                raise ValueError(
                    f"{', '.join(placeholders)} still hold the example value(s) from "
                    f".env.example. Set real secrets before running with "
                    f"ENVIRONMENT=production."
                )

        return self

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        """Whether the process is running in the production environment."""
        return self.environment is Environment.PRODUCTION

    @property
    def proxies(self) -> list[str]:
        """Proxy URLs parsed from :attr:`proxy_list`.

        Returns:
            One entry per non-empty, comma-separated proxy URL. Empty when no
            proxy is configured.
        """
        return [proxy.strip() for proxy in self.proxy_list.split(",") if proxy.strip()]

    @property
    def db_echo(self) -> bool:
        """Whether SQLAlchemy should echo emitted SQL.

        Enabled only at ``DEBUG``/``TRACE`` and never in production, where echoed
        statements would flood the logs and can leak row data.
        """
        return self.log_level in {"DEBUG", "TRACE"} and not self.is_production


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    The result is cached, so the environment is parsed and validated once per
    process. Tests that need a different configuration should call
    ``get_settings.cache_clear()`` or override the FastAPI dependency rather than
    mutating the returned object.

    Returns:
        The validated :class:`Settings` singleton.
    """
    return Settings()
