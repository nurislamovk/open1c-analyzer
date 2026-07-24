# Open1C Analyzer

Open1C Analyzer is a Python toolkit for indexing, analyzing, and preparing exported
1C:Enterprise source code for static analysis and LLM-assisted workflows.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended)

## Development setup

```bash
uv sync --extra dev
uv run open1c version
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

## PR-001 scope

- Python package skeleton
- SQLite storage via SQLAlchemy 2.x
- Alembic migrations
- Typer CLI
- Tests and CI
