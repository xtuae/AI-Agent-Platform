"""Application settings. Every value comes from the environment; nothing secret has a default."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "staging", "production"] = "local"
    app_version: str = "0.1.0"
    git_sha: str = "unknown"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Runtime connection: the `app` role. Owns nothing, subject to RLS.
    database_url: PostgresDsn
    # Migration connection: the owner role. Used only by Alembic and never by request/worker code.
    migrations_database_url: PostgresDsn | None = None
    db_pool_size: int = Field(default=10, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_statement_timeout_ms: int = Field(default=5_000, ge=100)
    db_connect_timeout_s: float = Field(default=5.0, gt=0)

    redis_url: RedisDsn
    redis_timeout_s: float = Field(default=2.0, gt=0)

    # Fernet key for Meta access tokens at rest (constraint 6).
    app_encryption_key: SecretStr

    # Meta — used from Phase 1, validated now so a misconfigured box fails at boot, not at 2am.
    meta_app_secret: SecretStr | None = None
    meta_verify_token: SecretStr | None = None
    meta_graph_api_version: str = "v21.0"
    meta_graph_base_url: str = "https://graph.facebook.com"
    meta_http_timeout_s: float = Field(default=10.0, gt=0)
    meta_connect_timeout_s: float = Field(default=5.0, gt=0)
    meta_max_retries: int = Field(default=3, ge=0, le=6)
    meta_media_max_bytes: int = Field(default=25 * 1024 * 1024, gt=0)

    # Webhook ingest
    webhook_route_cache_ttl_s: int = Field(default=300, ge=1)  # 01 §6.1: 5 min
    webhook_route_negative_ttl_s: int = Field(default=60, ge=1)
    webhook_dedup_ttl_s: int = Field(default=48 * 3600, ge=60)  # 01 §6.1: 48 h

    # LLM providers (01 §7): Gemini primary via its OpenAI-compatible endpoint → OpenRouter.
    google_ai_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    gemini_native_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model_prefix: str = "google/"
    llm_timeout_s: float = Field(default=20.0, gt=0)
    llm_connect_timeout_s: float = Field(default=5.0, gt=0)
    # USD per 1M tokens (input, output), keyed by model id without provider prefix. A model with
    # no entry is metered at 0 and logged as `llm_price_unknown` — fill this in, don't guess.
    llm_prices_usd_per_mtok: dict[str, tuple[Decimal, Decimal]] = Field(
        default_factory=lambda: {"gemini-2.5-flash-lite": (Decimal("0.10"), Decimal("0.40"))}
    )
    transcription_model: str = "gemini-2.5-flash"
    summary_model: str | None = None  # None → the tenant's classify model

    # Embeddings (01 §9): fastembed in-process, 384 dims.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str | None = None

    # Agent turn
    turn_debounce_s: float = Field(default=2.0, ge=0)  # coalesce messages sent seconds apart
    conversation_lock_ttl_s: int = Field(default=120, ge=10)
    support_max_tool_rounds: int = Field(default=5, ge=1, le=10)
    system_prompt_token_cap: int = Field(default=1500, ge=500)
    summary_every_n_inbound: int = Field(default=6, ge=1)
    llm_down_retry_defer_s: int = Field(default=60, ge=5)

    # --- dashboard auth (Phase 3) ---
    jwt_secret: SecretStr | None = None  # HS256 key, >= 32 bytes; auth answers 503 without it
    jwt_issuer: str = "hmh-agents"
    jwt_access_ttl_s: int = Field(default=900, ge=60, le=3600)  # 15 min
    jwt_refresh_ttl_s: int = Field(default=14 * 86400, ge=3600)  # 14 days, rotated on use
    login_max_failures: int = Field(default=10, ge=3)  # per email, per window
    login_failure_window_s: int = Field(default=900, ge=60)
    stream_heartbeat_s: float = Field(default=15.0, gt=0)

    # --- opt-in consent pages and the platform console (Phase 5) ---
    # Public origin of the /q/<code> consent pages (e.g. https://go.hmhagents.com); QR codes point
    # here. Unset → the origin the dashboard request came in on.
    optin_base_url: str | None = None
    optin_rate_limit: int = Field(default=30, ge=1)  # "Continue" taps per client, per window
    optin_rate_window_s: int = Field(default=600, ge=60)
    # AED is pegged to the dollar; used to show LLM spend (billed in USD) next to AED figures.
    usd_to_aed: Decimal = Decimal("3.6725")
    platform_access_ttl_s: int = Field(default=900, ge=60, le=3600)
    platform_session_ttl_s: int = Field(default=12 * 3600, ge=900)  # console sign-in lifetime

    # --- hardening (Phase 6) ---
    # Alerts to HMH Labz's own WhatsApp: sent from the channel of the tenant with this slug (HMH
    # Labz's own number), as the approved template below with one body variable, to each number.
    alert_tenant_slug: str | None = None
    alert_to: str | None = None  # comma-separated, E.164
    alert_template: str = "platform_alert"
    alert_template_language: str = "en"
    disk_check_paths: list[str] = Field(default_factory=lambda: ["/"])
    # Backups: a read-only BYPASSRLS role (libpq/asyncpg URL), an age PUBLIC key, S3-compatible
    # storage (Cloudflare R2 or Backblaze B2). The age private key never goes on the server.
    backup_database_url: SecretStr | None = None
    backup_age_recipient: str | None = None
    backup_bucket: str | None = None
    backup_endpoint_url: str | None = None
    backup_region: str = "auto"
    backup_access_key_id: str | None = None
    backup_secret_access_key: SecretStr | None = None
    backup_prefix: str = "hmh-agents"
    backup_retention_days: int = Field(default=30, ge=1)

    # concurrent jobs per worker process. Turns are I/O-bound (and sleep through the debounce),
    # so this is limited by the DB pool and LLM rate limits, not CPU. See RUNBOOK capacity notes.
    worker_max_jobs: int = Field(default=50, ge=1, le=500)

    arq_queue_name: str = "arq:queue"
    arq_scheduler_queue_name: str = "arq:scheduler"

    @field_validator("app_encryption_key")
    @classmethod
    def _valid_fernet_key(cls, v: SecretStr) -> SecretStr:
        try:
            Fernet(v.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "APP_ENCRYPTION_KEY must be a urlsafe base64 32-byte Fernet key"
            ) from exc
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _long_jwt_secret(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value().encode()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes")
        return v

    @field_validator("database_url", "migrations_database_url")
    @classmethod
    def _asyncpg_driver(cls, v: PostgresDsn | None) -> PostgresDsn | None:
        if v is not None and v.scheme != "postgresql+asyncpg":
            raise ValueError("database URLs must use the postgresql+asyncpg:// scheme")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # populated from the environment
