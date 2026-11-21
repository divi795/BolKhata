"""
BolKhata – Application Configuration
=====================================
Loads settings from environment variables or a .env file at project root.
All Gnani API credentials are kept here and never hardcoded elsewhere.

Usage:
    from app.core.config import settings
    print(settings.gnani_base_url)
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central settings object – all values come from .env or environment."""

    model_config = SettingsConfigDict(
        # Looks for .env in the project root (two levels above this file)
        env_file=".env",
        env_file_encoding="utf-8",
        # Silently ignore extra env vars that are not declared here
        extra="ignore",
        # Allow case-insensitive env var names (GNANI_API_KEY == gnani_api_key)
        case_sensitive=False,
    )

    # -------------------------------------------------------------------------
    # Application metadata
    # -------------------------------------------------------------------------
    app_name: str = Field(default="BolKhata", description="Human-readable app name")
    app_version: str = Field(default="0.1.0", description="Semantic version")
    debug: bool = Field(default=False, description="Enable debug logging")

    # -------------------------------------------------------------------------
    # Database
    # -------------------------------------------------------------------------
    database_url: str = Field(
        default="sqlite:///./bolkhata.db",
        description="SQLAlchemy-compatible database URL",
    )

    # -------------------------------------------------------------------------
    # Gnani Voice AI Platform  (Prisma v3.0 STT + Timbre v3.0 TTS)
    # -------------------------------------------------------------------------
    gnani_api_key: str = Field(
        default="",
        description="Gnani Vachana AI API key (X-API-Key-ID header value)",
    )
    gnani_base_url: str = Field(
        default="https://api.vachana.ai",
        description="Gnani REST API base URL",
    )
    gnani_ws_url: str = Field(
        default="wss://api.vachana.ai",
        description="Gnani WebSocket base URL for real-time streaming",
    )

    # Prisma v3.0 – Speech-to-Text
    gnani_stt_model: str = Field(
        default="prisma-v3.0",
        description="Gnani STT model identifier",
    )
    gnani_stt_language: str = Field(
        default="indic-auto",
        description=(
            "STT language code: 'indic-auto' for automatic detection, "
            "'hi-IN' for Hindi, 'en-IN' for Indian English"
        ),
    )
    gnani_stt_format: Literal["transcribe", "verbatim", "code-mix", "transliterate"] = Field(
        default="transcribe",
        description="Prisma v3.0 output format",
    )

    # Timbre v3.0 – Text-to-Speech
    gnani_tts_model: str = Field(
        default="timbre-v3.0",
        description="Gnani TTS model identifier",
    )
    gnani_tts_voice: str = Field(
        default="Shivani",
        description=(
            "Timbre v3.0 voice ID. Official supported voices include Shivani, "
            "Avinash, Lakshmi, and other Timbre v3.0 voices."
        ),
    )
    gnani_tts_language: str = Field(
        default="hi-IN",
        description="TTS language code: 'hi-IN' for Hindi, 'en-IN' for Indian English",
    )
    gnani_tts_speed: Literal[0.8, 1.0, 1.2] = Field(
        default=1.0,
        description="Timbre v3.0 speed. Supported values: 0.8, 1.0, 1.2.",
    )
    gnani_tts_sample_rate: int = Field(
        default=16000,
        description="Audio sample rate for TTS output in Hz (8000, 16000, 24000, 48000)",
    )

    # -------------------------------------------------------------------------
    # Voice integration behaviour
    # -------------------------------------------------------------------------
    gnani_mock_mode: bool = Field(
        default=False,
        description=(
            "When True (or when gnani_api_key is empty), the voice services return "
            "hard-coded mock responses so tests and offline development work without "
            "consuming real API credits."
        ),
    )

    @field_validator("gnani_mock_mode", mode="before")
    @classmethod
    def auto_enable_mock_when_no_key(cls, v: bool) -> bool:
        """
        Mock mode is explicitly set via GNANI_MOCK_MODE env var.
        The GnaniClient will also check gnani_api_key at runtime and
        fall back to mock behaviour when the key is empty.
        """
        return v

    # -------------------------------------------------------------------------
    # Convenience helpers
    # -------------------------------------------------------------------------
    @property
    def gnani_stt_ws_endpoint(self) -> str:
        """Full WebSocket URL for Prisma v3.0 real-time streaming STT."""
        return f"{self.gnani_ws_url}/stt/v3/stream"

    @property
    def gnani_stt_rest_endpoint(self) -> str:
        """Full REST URL for Prisma v3.0 short-clip STT."""
        return f"{self.gnani_base_url}/stt/v3"

    @property
    def gnani_tts_rest_endpoint(self) -> str:
        """Full REST URL for Timbre v3.0 TTS inference."""
        return f"{self.gnani_base_url}/api/v1/tts/inference"

    @property
    def gnani_tts_ws_endpoint(self) -> str:
        """Full WebSocket URL for Timbre v3.0 real-time TTS streaming."""
        return f"{self.gnani_ws_url}/api/v1/tts"

    @property
    def is_gnani_configured(self) -> bool:
        """True when a real (non-empty) Gnani API key is present."""
        return bool(self.gnani_api_key and self.gnani_api_key.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Returns a cached singleton Settings instance.
    Call get_settings.cache_clear() in tests to reload settings from a fresh env.
    """
    return Settings()


# Module-level singleton for convenient import:  from app.core.config import settings
settings: Settings = get_settings()
