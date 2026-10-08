"""Application settings loaded from environment variables (and `.env` in local development).

Every tunable lives here so that behaviour is configured, not hard-coded. Secrets are
`SecretStr` so they never appear in reprs, logs or tracebacks.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
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


class StorageBackendName(StrEnum):
    LOCAL = "local"
    S3 = "s3"


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

    # ---------------------------------------------------------------- HTTP limits
    # Request bodies on non-upload endpoints (JSON APIs).
    api_max_body_bytes: int = Field(default=1024 * 1024, ge=1024, le=50 * 1024 * 1024)

    # ---------------------------------------------------------------- uploads
    upload_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1024, le=500 * 1024 * 1024)
    upload_max_pages: int = Field(default=200, ge=1, le=5000)
    # 50 MP covers A4 at 600 DPI (~35 MP); larger images are rejected as potential bombs.
    upload_max_image_pixels: int = Field(default=50_000_000, ge=1_000_000, le=500_000_000)

    # ---------------------------------------------------------------- document storage
    storage_backend: StorageBackendName = StorageBackendName.LOCAL
    storage_local_root: Path = Path("storage")
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None  # e.g. Cloudflare R2, MinIO; None = AWS
    s3_region: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_key_prefix: str = ""

    # ---------------------------------------------------------------- background jobs
    worker_concurrency: int = Field(default=1, ge=1, le=32)
    worker_poll_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    job_lease_seconds: int = Field(default=300, ge=10, le=3600)
    job_max_attempts: int = Field(default=3, ge=1, le=20)
    job_retry_base_seconds: float = Field(default=30.0, ge=0, le=3600)
    # Touched by the worker on every loop; the container health check reads its age.
    worker_heartbeat_file: Path | None = None

    # ---------------------------------------------------------------- OCR & understanding
    tesseract_cmd: str = "tesseract"
    # Tesseract language codes joined with "+", e.g. "eng" or "eng+deu" (data must be installed).
    ocr_languages: str = Field(default="eng", pattern=r"^[A-Za-z_]+(\+[A-Za-z_]+)*$")
    ocr_dpi: int = Field(default=300, ge=150, le=600)
    # Images below this DPI are upscaled (max 2x) before OCR; 0 disables upscaling.
    ocr_upscale_below_dpi: int = Field(default=250, ge=0, le=600)
    ocr_page_timeout_seconds: float = Field(default=120.0, gt=0, le=1800)
    ocr_concurrency: int = Field(default=2, ge=1, le=16)
    ocr_remove_ruling_lines: bool = False
    # Pages whose mean OCR confidence is below this send the document to review.
    ocr_review_below_confidence: float = Field(default=50.0, ge=0, le=100)
    page_preview_width: int = Field(default=1000, ge=200, le=3000)
    classification_min_confidence: float = Field(default=0.7, ge=0, le=1)
    classification_llm_fallback: bool = True
    classification_corpus_per_class: int = Field(default=200, ge=20, le=2000)
    # Highest sensitivity whose content may be sent to an external AI provider (C1, ADR-007).
    ai_external_max_sensitivity: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"] = (
        "INTERNAL"
    )

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

    @field_validator("gemini_api_key", "s3_secret_access_key", mode="before")
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
        if self.storage_backend == StorageBackendName.S3 and not self.s3_bucket:
            msg = "S3_BUCKET is required when STORAGE_BACKEND=s3"
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
