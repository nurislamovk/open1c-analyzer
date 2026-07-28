"""Project-catalog CLI commands."""

from pathlib import Path
from typing import Annotated, Never

import typer
from rich.table import Table

from open1c_analyzer.cli.common import console, database_session
from open1c_analyzer.services.project_catalog import ProjectCatalog, ProjectCatalogError

project_app = typer.Typer(no_args_is_help=True, help="Manage registered 1C source projects.")


def _fail(message: str) -> Never:
    console.print(f"[red]ERROR[/red] {message}")
    raise typer.Exit(code=1)


@project_app.command("add")
def add_project(name: str, source_path: Path) -> None:
    """Register a 1C source-export directory."""
    try:
        with database_session() as session:
            project = ProjectCatalog(session).add_project(name, source_path)
            console.print(f"Project added: [bold]{project.name}[/bold]")
            console.print(f"Source: {project.source_path}")
    except ProjectCatalogError as exc:
        _fail(str(exc))


@project_app.command("list")
def list_projects() -> None:
    """List registered projects."""
    with database_session() as session:
        catalog = ProjectCatalog(session)
        projects = catalog.list_projects()
        if not projects:
            console.print("No projects registered.")
            return

        table = Table("Name", "Source directory", "Files", "Last scan")
        for project in projects:
            last_scan = project.last_scanned_at.isoformat() if project.last_scanned_at else "never"
            table.add_row(
                project.name,
                project.source_path or "not configured",
                str(catalog.file_count(project)),
                last_scan,
            )
        console.print(table)


@project_app.command("show")
def show_project(name: str) -> None:
    """Show one registered project."""
    try:
        with database_session() as session:
            catalog = ProjectCatalog(session)
            project = catalog.get_project(name)
            console.print(f"Name: {project.name}")
            console.print(f"UUID: {project.uuid}")
            console.print(f"Source: {project.source_path or 'not configured'}")
            console.print(f"Files: {catalog.file_count(project)}")
            last_scan = project.last_scanned_at.isoformat() if project.last_scanned_at else "never"
            console.print(f"Last scan: {last_scan}")
    except ProjectCatalogError as exc:
        _fail(str(exc))


@project_app.command("scan")
def scan_project(name: str) -> None:
    """Synchronize a project's file catalog with its source directory."""
    try:
        with database_session() as session:
            result = ProjectCatalog(session).scan_project(name)
        console.print(f"Project scanned: [bold]{name}[/bold]")
        console.print(
            f"Files: {result.total}; added: {result.added}; updated: {result.updated}; "
            f"unchanged: {result.unchanged}; removed: {result.removed}"
        )
    except ProjectCatalogError as exc:
        _fail(str(exc))


@project_app.command("remove")
def remove_project(
    name: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Remove a project from the catalog without touching its source files."""
    if not yes and not typer.confirm(f"Remove project '{name}' from the catalog?"):
        console.print("Cancelled.")
        return

    try:
        with database_session() as session:
            ProjectCatalog(session).remove_project(name)
        console.print(f"Project removed: {name}")
    except ProjectCatalogError as exc:
        _fail(str(exc))
