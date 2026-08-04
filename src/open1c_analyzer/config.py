"""Application configuration."""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="OPEN1C_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_path: Path = Field(default=Path(".open1c/open1c.db"))
    runs_path: Path = Field(default=Path(".open1c/runs"))
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("OPEN1C_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openai_model: str = Field(default="gpt-5.6")
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = Field(
        default="medium"
    )
    openai_timeout_seconds: float = Field(default=180.0, gt=0)
    openai_max_retries: int = Field(default=2, ge=0, le=10)

    @property
    def database_url(self) -> str:
        """Return a SQLAlchemy-compatible SQLite URL."""
        return f"sqlite:///{self.database_path.resolve().as_posix()}"
