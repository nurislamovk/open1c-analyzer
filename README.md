# Open1C Analyzer

Open1C Analyzer builds a structured, queryable knowledge graph from an exported
1C:Enterprise configuration. The graph is intended for impact analysis and for
providing verified project context to an LLM before it proposes 1C code changes.

## Requirements

- Python 3.12+
- `uv`

## Setup

```powershell
uv sync --extra dev
uv run open1c init
```

By default, local data is stored in `.open1c/open1c.db`. Override the path with
`OPEN1C_DATABASE_PATH`. Source files are read-only: the analyzer does not modify
the exported configuration.

## Analyzer Core workflow

Register an exported configuration and build the complete static index:

```powershell
uv run open1c project add TMS C:\Sources\TMS
uv run open1c project analyze TMS
uv run open1c project summary TMS
```

A repeated `analyze` is incremental: unchanged files are skipped. Use `--force`
for a complete rebuild.

```powershell
uv run open1c project analyze TMS --force
```

## Collected knowledge

The Analyzer Core stores:

- exported files, checksums, language and analysis status;
- top-level metadata objects and selected child objects from XML/MDO exports;
- configuration profile values such as compatibility mode when present in the export;
- BSL modules, procedures, functions, parameters, export flags, regions and directives;
- method calls with same-module, qualified and unique-export resolution;
- query text and table usage for read, write, update and delete operations;
- explicit references to documents, catalogs, registers and other metadata managers;
- resolved and unresolved dependency edges between metadata, modules, methods and queries.

This is static analysis. Dynamic execution, names assembled at runtime and some
platform-specific implicit relationships can remain unresolved; they are kept in
the database as unresolved facts instead of being guessed.

## Inspection commands

```powershell
uv run open1c project find TMS РассчитатьПотребность
uv run open1c project callers TMS РассчитатьПотребность
uv run open1c project callees TMS РассчитатьПотребность
uv run open1c project dependencies TMS РегистрНакопления.Запасы
uv run open1c project dependencies TMS РассчитатьПотребность --outgoing
uv run open1c project queries TMS
uv run open1c project queries TMS РегистрНакопления.Запасы
```

Export a structured snapshot for the future task-analysis and LLM layer:

```powershell
uv run open1c project snapshot TMS .open1c\tms-snapshot.json
```

## One-command verification

```powershell
uv run open1c check
```

The command applies safe Ruff fixes and formatting, then runs mypy and pytest.

## Current milestone

- repository, SQLite storage, Alembic migrations and CLI;
- project catalog and incremental file scanning;
- BSL and metadata parsing;
- call, query, reference and dependency graph construction;
- search, graph inspection, summary and JSON snapshot export.

The next layer will consume this graph to select context for a real task, perform
impact analysis and prepare an explainable 1C code-change plan and patch.
