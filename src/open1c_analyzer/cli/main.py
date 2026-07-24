"""Open1C Analyzer command-line interface."""

from pathlib import Path

import typer
from alembic import command
from alembic.config import Config
from rich.console import Console
from sqlalchemy import text

from open1c_analyzer.config import Settings
from open1c_analyzer.storage.database import Database
from open1c_analyzer.version import __version__

app = typer.Typer(no_args_is_help=True, help="Analyze exported 1C:Enterprise source code.")
console = Console()


def _alembic_config(settings: Settings) -> Config:
    migrations_path = Path(__file__).resolve().parents[1] / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migrations_path))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


@app.command()
def version() -> None:
    """Print the application version."""
    console.print(__version__)


@app.command()
def migrate() -> None:
    """Apply all database migrations."""
    settings = Settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic_config(settings), "head")
    console.print(f"Database migrated: {settings.database_path}")


@app.command()
def init() -> None:
    """Initialize the local application database."""
    migrate()
    console.print("Open1C Analyzer initialized.")


@app.command()
def doctor() -> None:
    """Check configuration and database connectivity."""
    settings = Settings()
    checks: list[tuple[str, bool, str]] = []

    checks.append(
        (
            "Database directory",
            settings.database_path.parent.exists(),
            str(settings.database_path.parent),
        )
    )

    try:
        database = Database(settings.database_url)
        try:
            with database.session() as session:
                session.execute(text("SELECT 1"))
            checks.append(("SQLite connection", True, settings.database_url))
        finally:
            database.dispose()
    except Exception as exc:  # pragma: no cover - defensive CLI reporting
        checks.append(("SQLite connection", False, str(exc)))

    failed = False
    for name, ok, detail in checks:
        marker = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
        console.print(f"{marker} {name}: {detail}")
        failed = failed or not ok

    if failed:
        raise typer.Exit(code=1)
