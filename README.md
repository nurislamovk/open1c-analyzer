# Open1C Analyzer

Open1C Analyzer is a Python toolkit for indexing, analyzing, and preparing exported
1C:Enterprise source code for static analysis and LLM-assisted workflows.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended)

## Development setup

```bash
uv sync --extra dev
uv run open1c init
uv run pytest
```

By default, application data is stored in `.open1c/open1c.db` in the current directory.
Override it with `OPEN1C_DATABASE_PATH`.

## CLI

```bash
open1c version
open1c init
open1c migrate
open1c doctor
```

### Project catalog

Register an exported 1C configuration directory and build its file catalog:

```bash
open1c project add TMS C:\Sources\TMS
open1c project scan TMS
open1c project list
open1c project show TMS
```

A repeated scan is idempotent. It records new and changed files, keeps unchanged files,
and removes database records for files that disappeared from the source directory. Source
files themselves are never modified.

Remove a project only from the analyzer catalog:

```bash
open1c project remove TMS --yes
```

## Implemented scope

### PR-001

- Python package skeleton
- SQLite storage via SQLAlchemy 2.x
- Alembic migrations
- Typer CLI
- Tests and CI

### PR-002

- Named project catalog with source directories
- Recursive source-file discovery
- SHA-256 checksums and language classification
- Idempotent rescan with added, updated, unchanged, and removed counters
- Project CLI commands and tests
