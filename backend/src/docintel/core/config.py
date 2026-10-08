"""Application settings loaded from environment variables (and `.env` in local development).

Every tunable lives here so that behaviour is configured, not hard-coded. Secrets are
`SecretStr` so they never appear in reprs, logs or tracebacks.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


class LLMProviderName(StrEnum):
    GEMINI = "gemini"


class EmbeddingProviderName(StrEnum):
    GEMINI = "gemini"


ThinkingLevel = Literal["MINIMAL", "LOW", "MEDIUM", "HIGH"]

JWT_SECRET_MIN_LENGTH = 32
_PLACEHOLDER_MARKERS = ("change", "replace", "example", "placeholder", "secret-key")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- application
    app_name: str = "Enterprise Document Intelligence Platform"
    app_env: Environment = Environment.LOCAL
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # None = derive from environment (console locally, JSON elsewhere).
    log_format: LogFormat | None = None
    # None = enabled everywhere except production.
    api_docs_enabled: bool | None = None
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ---------------------------------------------------------------- database
    database_url: str = Field(
        description="SQLAlchemy URL, must use the psycopg (v3) driver: postgresql+psycopg://..."
    )
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    db_statement_timeout_ms: int = Field(default=30_000, ge=100, le=600_000)
    db_echo: bool = False

    # ---------------------------------------------------------------- auth
    jwt_secret_key: SecretStr
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_access_token_ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)
    jwt_issuer: str = "docintel"
    jwt_audience: str = "docintel-api"
    auth_max_failed_logins: int = Field(default=5, ge=1, le=100)
    auth_lockout_minutes: int = Field(default=15, ge=1, le=24 * 60)

    # ---------------------------------------------------------------- AI providers
    llm_provider: LLMProviderName = LLMProviderName.GEMINI
    embedding_provider: EmbeddingProviderName = EmbeddingProviderName.GEMINI
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash"
    gemini_fast_model: str = "gemini-3.5-flash-lite"
    gemini_embedding_model: str = "gemini-embedding-001"
    # Gemini 3.x uses thinking levels; None = model default (parameter not sent).
    gemini_thinking_level: ThinkingLevel | None = None
    # None = model default (parameter not sent). Gemini 3.x guidance discourages overriding it.
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    llm_max_retries: int = Field(default=3, ge=0, le=10)
    llm_requests_per_minute: int = Field(default=10, ge=1, le=10_000)
    embedding_requests_per_minute: int = Field(default=60, ge=1, le=10_000)
    embedding_batch_size: int = Field(default=100, ge=1, le=100)

    # ---------------------------------------------------------------- CLI
    seed_user_password: SecretStr | None = None

    # ---------------------------------------------------------------- validators
    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+psycopg://"):
            msg = "DATABASE_URL must use the psycopg driver: postgresql+psycopg://user:pass@host/db"
            raise ValueError(msg)
        return value

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_secrets_for_environment(self) -> Self:
        secret = self.jwt_secret_key.get_secret_value()
        if len(secret) < JWT_SECRET_MIN_LENGTH:
            msg = f"JWT_SECRET_KEY must be at least {JWT_SECRET_MIN_LENGTH} characters"
            raise ValueError(msg)
        if self.is_deployed:
            lowered = secret.lower()
            if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
                msg = "JWT_SECRET_KEY looks like a placeholder; generate a random secret"
                raise ValueError(msg)
            if "*" in self.cors_allowed_origins:
                msg = "CORS_ALLOWED_ORIGINS must not contain '*' outside local/test"
                raise ValueError(msg)
        return self

    # ---------------------------------------------------------------- derived values
    @property
    def is_deployed(self) -> bool:
        """True for shared environments where insecure defaults must be refused."""
        return self.app_env in (Environment.STAGING, Environment.PRODUCTION)

    @property
    def effective_log_format(self) -> LogFormat:
        if self.log_format is not None:
            return self.log_format
        return LogFormat.CONSOLE if self.app_env == Environment.LOCAL else LogFormat.JSON

    @property
    def effective_api_docs_enabled(self) -> bool:
        if self.api_docs_enabled is not None:
            return self.api_docs_enabled
        return self.app_env != Environment.PRODUCTION

    @property
    def hsts_enabled(self) -> bool:
        return self.is_deployed


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (environment is read once)."""
    return Settings()  # required fields come from the environment
