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

For an extension, resolve calls and metadata references against the indexed base
configuration:

```powershell
uv run open1c project resolve TMS_EXT --include TMS_UNF
```

The same option is available on incremental analysis, so changed extension files
can be indexed and linked in one command:

```powershell
uv run open1c project analyze TMS_EXT --include TMS_UNF
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

A separately indexed extension can be included in focused retrieval. Run
`project resolve` with the base configuration first to persist conservative
cross-project links. Extension symbols take precedence; the base configuration is
used only when the extension has no matching local or qualified export.

```powershell
uv run open1c project resolve TMS_EXT --include TMS_UNF
uv run open1c find TMS_EXT "ОбработкаПроведения" --include TMS_UNF
uv run open1c calls TMS_EXT "Запустить" --include TMS_UNF --direction outgoing
uv run open1c context TMS_EXT "Запустить" --include TMS_UNF `
    --output .open1c\tms-context.json
```

For databases created before explicit linking, focused retrieval still surfaces a
unique unresolved target as `cross_project_candidate` until `project resolve --include`
is run.

The earlier project-scoped inspection commands remain available for compatibility:

```powershell
uv run open1c project find TMS РассчитатьПотребность
uv run open1c project callers TMS РассчитатьПотребность
uv run open1c project callees TMS РассчитатьПотребность
uv run open1c project dependencies TMS РегистрНакопления.Запасы
uv run open1c project queries TMS РегистрНакопления.Запасы
```

## LLM Integration Core

Configure an OpenAI API key. The standard OpenAI variable and an Open1C-specific
alias are both supported:

```powershell
$env:OPENAI_API_KEY = "sk-..."
# or: $env:OPEN1C_OPENAI_API_KEY = "sk-..."
```

Ask a natural-language engineering question. The command ranks indexed entities,
selects an entry point, builds bounded source-backed context, calls the OpenAI
Responses API and prints an answer with source locations:

```powershell
uv run open1c ask TMS_EXT `
    "Где и как расширение изменяет печатную форму счета на оплату?" `
    --include TMS_UNF
```

Use `--term` when the task has an exact technical entry point:

```powershell
uv run open1c ask TMS_EXT `
    "Объясни изменение печатной формы и риски обновления типовой УНФ" `
    --term "Обработка.ПечатьСчетНаОплату" `
    --include TMS_UNF
```

The default model is `gpt-5.6` with medium reasoning. Override it with `--model`
and `--reasoning`, or environment variables `OPEN1C_OPENAI_MODEL` and
`OPEN1C_OPENAI_REASONING_EFFORT`. API requests are sent with `store=false`.

Every run is reproducible and stored under `.open1c/runs/<timestamp>-<question>/`:

- `request.json` — question, retrieval plan, model settings and SHA-256 hashes;
- `context.json` — complete Open1C Analyzer context package;
- `prompt.txt` — exact instructions and bounded prompt sent to the provider;
- `answer.md` — model answer;
- `run.json` — status, request/response IDs and token usage.

Prepare and inspect all artifacts without sending source code to OpenAI:

```powershell
uv run open1c ask TMS_EXT `
    "Где изменяется печать счета на оплату?" `
    --include TMS_UNF `
    --dry-run
```

When inspecting UTF-8 run artifacts from Windows PowerShell 5.1, specify the
encoding explicitly to avoid mojibake:

```powershell
$request = Get-Content ".open1c\runs\<run>\request.json" -Raw -Encoding UTF8 |
    ConvertFrom-Json
```

The prompt builder preserves exact matches and selected source units first, then
trims graph edges and unresolved-call noise to fit `--max-prompt-chars`. Use
`--max-chars`, `--max-units` and `--max-output-tokens` to control the budget.

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
