"""Open1C Analyzer command-line interface."""

import typer
from sqlalchemy import text

from open1c_analyzer.cli.common import console, database_session, migrate_database
from open1c_analyzer.cli.project import project_app
from open1c_analyzer.config import Settings
from open1c_analyzer.version import __version__

app = typer.Typer(no_args_is_help=True, help="Analyze exported 1C:Enterprise source code.")
app.add_typer(project_app, name="project")


@app.command()
def version() -> None:
    """Print the application version."""
    console.print(__version__)


@app.command()
def migrate() -> None:
    """Apply all database migrations."""
    settings = Settings()
    migrate_database(settings)
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
        with database_session() as session:
            session.execute(text("SELECT 1"))
        checks.append(("SQLite connection", True, settings.database_url))
    except Exception as exc:  # pragma: no cover - defensive CLI reporting
        checks.append(("SQLite connection", False, str(exc)))

    failed = False
    for name, ok, detail in checks:
        marker = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
        console.print(f"{marker} {name}: {detail}")
        failed = failed or not ok

    if failed:
        raise typer.Exit(code=1)
