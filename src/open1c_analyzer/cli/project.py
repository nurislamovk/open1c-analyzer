"""Project catalog and analyzer CLI commands."""

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Never

import typer
from rich.table import Table

from open1c_analyzer.cli.common import console, database_session
from open1c_analyzer.services.analyzer import AnalyzerCore
from open1c_analyzer.services.knowledge import KnowledgeService
from open1c_analyzer.services.project_catalog import ProjectCatalog, ProjectCatalogError

project_app = typer.Typer(no_args_is_help=True, help="Manage and analyze 1C source projects.")


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
        table = Table("Name", "Source directory", "Files", "Last scan", "Last analysis")
        for project in projects:
            table.add_row(
                project.name,
                project.source_path or "not configured",
                str(catalog.file_count(project)),
                project.last_scanned_at.isoformat() if project.last_scanned_at else "never",
                project.last_analyzed_at.isoformat() if project.last_analyzed_at else "never",
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
            last_analysis = (
                project.last_analyzed_at.isoformat() if project.last_analyzed_at else "never"
            )
            console.print(f"Last scan: {last_scan}")
            console.print(f"Last analysis: {last_analysis}")
            console.print(f"Profile: {project.profile_json or '{}'}")
    except ProjectCatalogError as exc:
        _fail(str(exc))


@project_app.command("scan")
def scan_project(name: str) -> None:
    """Synchronize a project's file catalog."""
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


@project_app.command("analyze")
def analyze_project(
    name: str,
    force: Annotated[bool, typer.Option("--force", help="Re-analyze unchanged files.")] = False,
    no_scan: Annotated[
        bool, typer.Option("--no-scan", help="Do not refresh the file catalog.")
    ] = False,
) -> None:
    """Collect metadata, BSL, calls, queries and dependency edges."""
    try:
        with database_session() as session:
            result = AnalyzerCore(session).analyze_project(name, force=force, scan=not no_scan)
        console.print(f"Project analyzed: [bold]{name}[/bold]")
        console.print(
            f"Scan changes: +{result.scanned_added} "
            f"~{result.scanned_updated} "
            f"-{result.scanned_removed}"
        )
        console.print(
            f"Files: analyzed {result.analyzed_files}; "
            f"skipped {result.skipped_files}; "
            f"errors {len(result.errors)}"
        )
        console.print(
            f"Knowledge: metadata {result.metadata_objects}; modules {result.modules}; "
            f"symbols {result.symbols}; calls {result.calls}; queries {result.queries}; "
            f"dependencies {result.dependencies}"
        )
        console.print(f"Graph: {'rebuilt' if result.graph_rebuilt else 'reused'}")
        console.print(f"Unresolved calls: {result.unresolved_calls}")
        for error in result.errors:
            console.print(f"[yellow]WARN[/yellow] {error}")
    except ProjectCatalogError as exc:
        _fail(str(exc))


@project_app.command("summary")
def summary(name: str) -> None:
    """Show analysis readiness counters."""
    try:
        with database_session() as session:
            result = KnowledgeService(session).summary(name)
            table = Table("Metric", "Value")
            for key, value in asdict(result).items():
                table.add_row(key.replace("_", " "), str(value))
            console.print(table)
    except ProjectCatalogError as exc:
        _fail(str(exc))


@project_app.command("find")
def find(name: str, term: str) -> None:
    """Find metadata objects and BSL methods."""
    try:
        with database_session() as session:
            hits = KnowledgeService(session).find(name, term)
            if not hits:
                console.print("Nothing found.")
                return
            table = Table("Kind", "Name", "Location", "Details")
            for hit in hits:
                table.add_row(hit.kind, hit.name, hit.location, hit.details)
            console.print(table)
    except ProjectCatalogError as exc:
        _fail(str(exc))


def _print_calls(name: str, term: str, incoming: bool) -> None:
    try:
        with database_session() as session:
            service = KnowledgeService(session)
            rows = service.callers(name, term) if incoming else service.callees(name, term)
            if not rows:
                console.print("No calls found.")
                return
            table = Table("Caller", "Callee", "Resolution", "Location")
            for row in rows:
                table.add_row(row.caller, row.callee, row.resolution, f"{row.file}:{row.line}")
            console.print(table)
    except ProjectCatalogError as exc:
        _fail(str(exc))


@project_app.command("callers")
def callers(name: str, symbol: str) -> None:
    """Show methods calling a matching method."""
    _print_calls(name, symbol, True)


@project_app.command("callees")
def callees(name: str, symbol: str) -> None:
    """Show calls made by matching methods."""
    _print_calls(name, symbol, False)


@project_app.command("dependencies")
def dependencies(
    name: str,
    term: str,
    outgoing: Annotated[bool, typer.Option("--outgoing", help="Match graph sources.")] = False,
) -> None:
    """Show incoming or outgoing dependency edges."""
    try:
        with database_session() as session:
            rows = KnowledgeService(session).dependencies(name, term, incoming=not outgoing)
            if not rows:
                console.print("No dependencies found.")
                return
            table = Table("Source", "Relation", "Target", "Resolved", "Location")
            for row in rows:
                table.add_row(
                    row.source_name,
                    row.relation,
                    row.target_name,
                    "yes" if row.is_resolved else "no",
                    f"{row.source_file_id or '-'}:{row.line or '-'}",
                )
            console.print(table)
    except ProjectCatalogError as exc:
        _fail(str(exc))


@project_app.command("queries")
def queries(name: str, table_name: str | None = None) -> None:
    """List query table usage, optionally filtered by table."""
    try:
        with database_session() as session:
            rows = KnowledgeService(session).query_usage(name, table_name)
            if not rows:
                console.print("No query usage found.")
                return
            table = Table("Table", "Operation", "Symbol", "Location", "Resolved")
            for row in rows:
                table.add_row(
                    str(row["table"]),
                    str(row["operation"]),
                    str(row["symbol"] or "<module>"),
                    f"{row['file']}:{row['line']}",
                    "yes" if row["resolved"] else "no",
                )
            console.print(table)
    except ProjectCatalogError as exc:
        _fail(str(exc))


@project_app.command("snapshot")
def snapshot(name: str, output: Path = Path("open1c-snapshot.json")) -> None:
    """Export structured project knowledge for later task processing."""
    try:
        with database_session() as session:
            path = KnowledgeService(session).export_snapshot(name, output)
        console.print(f"Snapshot written: {path}")
    except ProjectCatalogError as exc:
        _fail(str(exc))


@project_app.command("remove")
def remove_project(
    name: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Remove a project from the catalog without touching source files."""
    if not yes and not typer.confirm(f"Remove project '{name}' from the catalog?"):
        console.print("Cancelled.")
        return
    try:
        with database_session() as session:
            ProjectCatalog(session).remove_project(name)
        console.print(f"Project removed: {name}")
    except ProjectCatalogError as exc:
        _fail(str(exc))
