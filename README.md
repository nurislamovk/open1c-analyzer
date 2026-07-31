# Open1C Analyzer

Open1C Analyzer builds a structured, queryable knowledge graph from an exported
1C:Enterprise configuration. The graph supports impact analysis and provides a
small, source-backed context package to an LLM before it proposes 1C code changes.

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

## Analyzer workflow

Register an exported configuration and build the complete static index:

```powershell
uv run open1c project add TMS C:\Sources\TMS
uv run open1c project analyze TMS
uv run open1c project summary TMS
```

A repeated `analyze` is incremental: unchanged files and the existing graph are
reused. Use `--force` for a complete rebuild.

```powershell
uv run open1c project analyze TMS --force
```

After upgrading the resolution engine, rebuild only call resolution and graph
edges without rereading BSL/XML files:

```powershell
uv run open1c project resolve TMS
```

## Collected knowledge

The analyzer stores:

- exported files, checksums, language and analysis status;
- top-level metadata objects and selected child objects from XML/MDO exports;
- configuration profile values such as compatibility mode when present;
- BSL modules, procedures, functions, parameters, export flags, regions and directives;
- method calls with local, common-module, qualified, platform, dynamic and ambiguous classification;
- query text and table usage for read, write, update and delete operations;
- explicit references to documents, catalogs, registers and other metadata managers;
- resolved and unresolved dependency edges between metadata, modules, methods and queries.

This is static analysis. Dynamic execution, names assembled at runtime and some
platform-specific implicit relationships remain classified instead of being guessed.

## Resolution & Retrieval Core

Search metadata, modules and methods:

```powershell
uv run open1c find TMS "РассчитатьПотребности"
```

Inspect incoming/outgoing calls and a bounded call chain:

```powershell
uv run open1c calls TMS "РассчитатьПотребности" --direction both --depth 2
```

Inspect incoming impact through resolved dependencies:

```powershell
uv run open1c impact TMS "РегистрНакопления.Запасы" --depth 3
```

Classify unresolved calls:

```powershell
uv run open1c audit TMS --group-by reason
uv run open1c audit TMS --group-by name --limit 100
uv run open1c audit TMS --group-by qualifier --limit 100
```

Build a compact context package containing only matching methods, nearby source,
call edges, dependencies, relevant queries and unresolved calls near the selected
code:

```powershell
uv run open1c context TMS "РассчитатьПотребности" `
    --depth 2 `
    --max-chars 60000 `
    --output .open1c\tms-context.json
```

The context schema is `open1c-analyzer-context-v1`. It is bounded by source-unit
and character limits and is intended for a concrete engineering task, unlike the
full project snapshot.

A separately indexed extension can be included in focused retrieval. Cross-project
calls are surfaced only when the target method is unique; they are marked as
`cross_project_candidate` rather than silently persisted as a certain edge.

```powershell
uv run open1c find TMS_UNF "ОбработкаПроведения" --include TMS_EXT
uv run open1c calls TMS_EXT "Запустить" --include TMS_UNF --direction outgoing
uv run open1c context TMS_UNF "РассчитатьПотребности" --include TMS_EXT `
    --output .open1c\tms-context.json
```

The earlier project-scoped inspection commands remain available for compatibility:

```powershell
uv run open1c project find TMS РассчитатьПотребность
uv run open1c project callers TMS РассчитатьПотребность
uv run open1c project callees TMS РассчитатьПотребность
uv run open1c project dependencies TMS РегистрНакопления.Запасы
uv run open1c project queries TMS РегистрНакопления.Запасы
```

## Full snapshot

A complete snapshot is still available, but it can be very large for a production
configuration:

```powershell
uv run open1c project snapshot TMS .open1c\tms-snapshot.json
```

## One-command verification

```powershell
uv run open1c check
```

The command applies safe Ruff fixes and formatting, then runs mypy and pytest.
