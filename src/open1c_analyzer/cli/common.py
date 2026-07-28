"""Shared CLI infrastructure."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from rich.console import Console
from sqlalchemy.orm import Session

from open1c_analyzer.config import Settings
from open1c_analyzer.storage.database import Database

console = Console()


def alembic_config(settings: Settings) -> Config:
    """Build an Alembic configuration for packaged migrations."""
    migrations_path = Path(__file__).resolve().parents[1] / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migrations_path))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def migrate_database(settings: Settings) -> None:
    """Apply all migrations to the configured database."""
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings), "head")


@contextmanager
def database_session() -> Iterator[Session]:
    """Yield one transactional application database session."""
    settings = Settings()
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            yield session
    finally:
        database.dispose()
