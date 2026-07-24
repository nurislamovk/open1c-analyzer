"""Application configuration."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="OPEN1C_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_path: Path = Field(default=Path(".open1c/open1c.db"))

    @property
    def database_url(self) -> str:
        """Return a SQLAlchemy-compatible SQLite URL."""
        return f"sqlite:///{self.database_path.resolve().as_posix()}"
