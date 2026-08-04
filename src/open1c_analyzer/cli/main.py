"""Open1C Analyzer command-line interface."""

import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Never, cast

import typer
from rich.markdown import Markdown
from rich.table import Table
from sqlalchemy import text

from open1c_analyzer.cli.common import console, database_session, migrate_database
from open1c_analyzer.cli.project import project_app
from open1c_analyzer.config import Settings
from open1c_analyzer.services.ask import AskError, AskService, ReasoningEffort
from open1c_analyzer.services.project_catalog import ProjectCatalogError
from open1c_analyzer.services.retrieval import RetrievalService
from open1c_analyzer.version import __version__

app = typer.Typer(no_args_is_help=True, help="Analyze exported 1C:Enterprise source code.")
app.add_typer(project_app, name="project")


def _fail(message: str) -> Never:
    console.print(f"[red]ERROR[/red] {message}")
    raise typer.Exit(code=1)


def _include_tuple(values: list[str] | None) -> tuple[str, ...]:
    return tuple(values or ())


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
    checks: list[tuple[str, bool, str]] = [
        (
            "Database directory",
            settings.database_path.parent.exists(),
            str(settings.database_path.parent),
        )
    ]
    try:
        with database_session() as session:
            session.execute(text("SELECT 1"))
        checks.append(("SQLite connection", True, settings.database_url))
    except Exception as exc:  # pragma: no cover
        checks.append(("SQLite connection", False, str(exc)))
    failed = False
    for name, ok, detail in checks:
        console.print(f"{'[green]OK[/green]' if ok else '[red]FAIL[/red]'} {name}: {detail}")
        failed = failed or not ok
    if failed:
        raise typer.Exit(code=1)


@app.command("find")
def find_entities(
    project: str,
    term: str,
    include: Annotated[
        list[str] | None,
        typer.Option(
            "--include", help="Include another analyzed project, for example an extension."
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
) -> None:
    """Find metadata objects, modules and BSL methods."""
    try:
        with database_session() as session:
            rows = RetrievalService(session).find(
                project,
                term,
                include=_include_tuple(include),
                limit=limit,
            )
        if not rows:
            console.print("Nothing found.")
            return
        table = Table("Project", "Kind", "Name", "Location", "Details")
        for row in rows:
            location = row.file or "-"
            if row.line_start is not None:
                location = f"{location}:{row.line_start}"
            table.add_row(row.project, row.kind, row.name, location, row.details)
        console.print(table)
    except ProjectCatalogError as exc:
        _fail(str(exc))


@app.command("calls")
def calls(
    project: str,
    term: str,
    direction: Annotated[
        str,
        typer.Option("--direction", "-d", help="incoming, outgoing or both"),
    ] = "both",
    depth: Annotated[int, typer.Option("--depth", min=1, max=10)] = 1,
    include: Annotated[
        list[str] | None,
        typer.Option("--include", help="Include another analyzed project."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=5000)] = 200,
) -> None:
    """Show incoming and outgoing call chains for matching methods."""
    if direction not in {"incoming", "outgoing", "both"}:
        _fail("Direction must be incoming, outgoing or both.")
    try:
        with database_session() as session:
            rows = RetrievalService(session).calls(
                project,
                term,
                direction=direction,  # type: ignore[arg-type]
                depth=depth,
                include=_include_tuple(include),
                limit=limit,
            )
        if not rows:
            console.print("No calls found.")
            return
        table = Table("Depth", "Project", "Caller", "Callee", "Resolution", "Location")
        for row in rows:
            project_label = (
                f"{row.project} → {row.callee_project}"
                if row.callee_project and row.callee_project != row.project
                else row.project
            )
            table.add_row(
                str(row.depth),
                project_label,
                row.caller,
                row.callee,
                row.resolution,
                f"{row.file}:{row.line}",
            )
        console.print(table)
    except ProjectCatalogError as exc:
        _fail(str(exc))


@app.command("impact")
def impact(
    project: str,
    term: str,
    depth: Annotated[int, typer.Option("--depth", min=1, max=10)] = 2,
    include: Annotated[
        list[str] | None,
        typer.Option("--include", help="Include another analyzed project."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=5000)] = 500,
) -> None:
    """Show incoming dependency chains affected by an object or method."""
    try:
        with database_session() as session:
            rows = RetrievalService(session).impact(
                project,
                term,
                depth=depth,
                include=_include_tuple(include),
                limit=limit,
            )
        if not rows:
            console.print("No impact edges found.")
            return
        table = Table(
            "Depth",
            "Project",
            "Source",
            "Relation",
            "Target",
            "Resolved",
            "Location",
        )
        for row in rows:
            location = row.file or "-"
            if row.line is not None:
                location = f"{location}:{row.line}"
            table.add_row(
                str(row.depth),
                row.project,
                row.source,
                row.relation,
                row.target,
                "yes" if row.resolved else "no",
                location,
            )
        console.print(table)
    except ProjectCatalogError as exc:
        _fail(str(exc))


@app.command("context")
def context(
    project: str,
    term: str,
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Compact JSON context file."),
    ] = Path("open1c-context.json"),
    depth: Annotated[int, typer.Option("--depth", min=1, max=5)] = 1,
    max_chars: Annotated[
        int,
        typer.Option("--max-chars", min=1000, max=1_000_000),
    ] = 60_000,
    max_units: Annotated[int, typer.Option("--max-units", min=1, max=200)] = 30,
    include: Annotated[
        list[str] | None,
        typer.Option("--include", help="Include another analyzed project."),
    ] = None,
) -> None:
    """Build a bounded, source-backed JSON context package for an LLM task."""
    try:
        with database_session() as session:
            path = RetrievalService(session).context(
                project,
                term,
                output,
                include=_include_tuple(include),
                depth=depth,
                max_chars=max_chars,
                max_units=max_units,
            )
        console.print(f"Context written: {path}")
    except ProjectCatalogError as exc:
        _fail(str(exc))


@app.command("ask")
def ask(
    project: str,
    question: str,
    include: Annotated[
        list[str] | None,
        typer.Option("--include", help="Include another analyzed project."),
    ] = None,
    term: Annotated[
        str | None,
        typer.Option("--term", help="Explicit metadata object, module or method entry point."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="OpenAI model ID. Defaults to OPEN1C_OPENAI_MODEL."),
    ] = None,
    reasoning: Annotated[
        str | None,
        typer.Option("--reasoning", help="none, low, medium, high, xhigh or max"),
    ] = None,
    depth: Annotated[int, typer.Option("--depth", min=1, max=5)] = 2,
    max_chars: Annotated[
        int,
        typer.Option("--max-chars", min=1000, max=1_000_000),
    ] = 60_000,
    max_units: Annotated[int, typer.Option("--max-units", min=1, max=200)] = 30,
    max_prompt_chars: Annotated[
        int,
        typer.Option("--max-prompt-chars", min=20_000, max=4_000_000),
    ] = 180_000,
    max_output_tokens: Annotated[
        int,
        typer.Option("--max-output-tokens", min=100, max=128_000),
    ] = 4_000,
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Directory for reproducible ask run artifacts."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Build plan/context/prompt without calling OpenAI."),
    ] = False,
) -> None:
    """Answer a 1C engineering question from indexed source with OpenAI."""
    allowed_reasoning = {"none", "low", "medium", "high", "xhigh", "max"}
    if reasoning is not None and reasoning not in allowed_reasoning:
        _fail("Reasoning must be none, low, medium, high, xhigh or max.")
    try:
        with database_session() as session:
            result = AskService(session).ask(
                project,
                question,
                include=_include_tuple(include),
                term=term,
                model=model,
                reasoning_effort=cast(ReasoningEffort | None, reasoning),
                depth=depth,
                max_chars=max_chars,
                max_units=max_units,
                max_prompt_chars=max_prompt_chars,
                max_output_tokens=max_output_tokens,
                output_root=output_root,
                dry_run=dry_run,
            )
        console.print(f"Selected entry point: [bold]{result.selected_term}[/bold]")
        console.print(f"Run directory: {result.run_directory}")
        console.print(f"Context: {result.context_path}")
        console.print(f"Prompt: {result.prompt_path}")
        if result.dry_run:
            console.print("[yellow]Dry run: no OpenAI request was sent.[/yellow]")
            return
        if result.answer:
            console.print()
            console.print(Markdown(result.answer))
        if result.answer_path:
            console.print(f"Answer: {result.answer_path}")
        if result.total_tokens is not None:
            console.print(
                f"Tokens: input {result.input_tokens or 0}; "
                f"output {result.output_tokens or 0}; total {result.total_tokens}"
            )
        if result.request_id:
            console.print(f"OpenAI request ID: {result.request_id}")
    except (AskError, ProjectCatalogError) as exc:
        _fail(str(exc))


@app.command("audit")
def audit(
    project: str,
    group_by: Annotated[
        str,
        typer.Option("--group-by", help="reason, name or qualifier"),
    ] = "reason",
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 50,
) -> None:
    """Classify unresolved calls and unresolved dependencies."""
    if group_by not in {"reason", "name", "qualifier"}:
        _fail("Group must be reason, name or qualifier.")
    try:
        with database_session() as session:
            report = RetrievalService(session).audit(
                project,
                group_by=group_by,  # type: ignore[arg-type]
                limit=limit,
            )
        counters = Table("Metric", "Value")
        for key, value in asdict(report).items():
            if key != "groups":
                counters.add_row(key.replace("_", " "), str(value))
        console.print(counters)
        groups = Table("Category", "Value", "Count")
        for row in report.groups:
            groups.add_row(row.category, row.value, str(row.count))
        console.print(groups)
    except ProjectCatalogError as exc:
        _fail(str(exc))


@app.command("check")
def check_project() -> None:
    """Run lint, formatting, type checks and tests with one command."""
    commands = [
        ("Ruff format", [sys.executable, "-m", "ruff", "format", "."]),
        ("Ruff autofix", [sys.executable, "-m", "ruff", "check", "--fix", "."]),
        ("Ruff verify", [sys.executable, "-m", "ruff", "check", "."]),
        ("mypy", [sys.executable, "-m", "mypy", "src"]),
        ("pytest", [sys.executable, "-m", "pytest"]),
    ]
    for label, command in commands:
        console.print(f"[bold]{label}[/bold]")
        completed = subprocess.run(command, check=False)
        if completed.returncode:
            raise typer.Exit(code=completed.returncode)
    console.print("[green]All checks passed.[/green]")
