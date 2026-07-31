"""End-to-end static indexing and dependency-graph construction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from open1c_analyzer.parser import BslAnalyzer, MetadataParser, identify_module, read_text
from open1c_analyzer.parser.names import full_metadata_name, normalize
from open1c_analyzer.services.project_catalog import ProjectCatalog, SourceDirectoryError
from open1c_analyzer.storage.models import (
    CallSite,
    Dependency,
    MetadataObject,
    Module,
    Project,
    QueryFact,
    Reference,
    SourceFile,
    Symbol,
)

_BUILTINS = {
    normalize(item)
    for item in (
        "Вопрос",
        "Вычислить",
        "Дата",
        "ДобавитьМесяц",
        "ЗаполнитьЗначенияСвойств",
        "ЗначениеЗаполнено",
        "Макс",
        "Мин",
        "Найти",
        "НачалоДня",
        "НачалоМесяца",
        "НСтр",
        "ОписаниеОшибки",
        "Окр",
        "Предупреждение",
        "ПустаяСтрока",
        "СокрЛП",
        "Сообщить",
        "СтрДлина",
        "СтрЗаменить",
        "СтрНайти",
        "СтрРазделить",
        "СтрСоединить",
        "ТекущаяДата",
        "ТекущаяДатаСеанса",
        "Тип",
        "ТипЗнч",
        "Формат",
        "Число",
        "String",
        "Number",
        "Type",
    )
}


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    scanned_added: int
    scanned_updated: int
    scanned_removed: int
    analyzed_files: int
    skipped_files: int
    metadata_objects: int
    modules: int
    symbols: int
    calls: int
    unresolved_calls: int
    queries: int
    dependencies: int
    graph_rebuilt: bool
    errors: tuple[str, ...]


class AnalyzerCore:
    """Build the facts required for later task analysis and LLM context selection."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.catalog = ProjectCatalog(session)
        self.bsl = BslAnalyzer()
        self.metadata = MetadataParser()

    def analyze_project(
        self, name: str, *, force: bool = False, scan: bool = True
    ) -> AnalysisResult:
        project = self.catalog.get_project(name)
        if not project.source_path:
            raise SourceDirectoryError(f"Project has no source directory: {name}")
        root = Path(project.source_path)
        scan_result = self.catalog.scan_project(name) if scan else None
        analyzed = 0
        skipped = 0
        errors: list[str] = []
        profile = self._profile(project)

        for source_file in self.catalog.list_files(project):
            if not self._analyzable(source_file):
                continue
            if (
                not force
                and source_file.analyzed_checksum == source_file.checksum
                and not source_file.analysis_error
            ):
                skipped += 1
                continue
            relative_path = source_file.relative_path
            try:
                with self.session.begin_nested():
                    self._clear_file(source_file.id)
                    source_path = root.joinpath(*PurePosixPath(relative_path).parts)
                    text = read_text(source_path)
                    if source_file.language == "bsl":
                        self._store_bsl(project, source_file, text)
                    else:
                        profile.update(self._store_metadata(project, source_file, text))
                    source_file.analyzed_checksum = source_file.checksum
                    source_file.analyzed_at = datetime.now(UTC)
                    source_file.analysis_error = None
                analyzed += 1
            except Exception as exc:
                message = f"{relative_path}: {type(exc).__name__}: {exc}"
                source_file.analysis_error = message
                source_file.analyzed_checksum = None
                errors.append(message)

        project.profile_json = (
            json.dumps(profile, ensure_ascii=False, sort_keys=True) if profile else None
        )
        graph_rebuilt = force or analyzed > 0 or bool(scan_result and scan_result.removed)
        if graph_rebuilt:
            self._resolve(project)
        project.last_analyzed_at = datetime.now(UTC)
        self.session.flush()
        counts = self._counts(project.id)
        unresolved = int(
            self.session.scalar(
                select(func.count(CallSite.id)).where(
                    CallSite.project_id == project.id, CallSite.resolution == "unresolved"
                )
            )
            or 0
        )
        return AnalysisResult(
            scan_result.added if scan_result else 0,
            scan_result.updated if scan_result else 0,
            scan_result.removed if scan_result else 0,
            analyzed,
            skipped,
            counts["metadata_objects"],
            counts["modules"],
            counts["symbols"],
            counts["calls"],
            unresolved,
            counts["queries"],
            counts["dependencies"],
            graph_rebuilt,
            tuple(errors),
        )

    @staticmethod
    def _analyzable(source_file: SourceFile) -> bool:
        if source_file.language == "bsl":
            return True
        if source_file.language == "mdo":
            return True
        if source_file.language != "xml":
            return False
        path = PurePosixPath(source_file.relative_path)
        if path.name.casefold() == "configuration.xml":
            return True
        return len(path.parts) == 2 and path.suffix.casefold() == ".xml"

    def _clear_file(self, file_id: int) -> None:
        self.session.execute(delete(Reference).where(Reference.source_file_id == file_id))
        self.session.execute(delete(CallSite).where(CallSite.source_file_id == file_id))
        self.session.execute(delete(QueryFact).where(QueryFact.source_file_id == file_id))
        self.session.execute(delete(Symbol).where(Symbol.source_file_id == file_id))
        self.session.execute(delete(Module).where(Module.source_file_id == file_id))
        self.session.execute(delete(MetadataObject).where(MetadataObject.source_file_id == file_id))
        self.session.flush()

    def _store_bsl(self, project: Project, source_file: SourceFile, text: str) -> None:
        parsed = self.bsl.parse(text)
        identity = identify_module(source_file.relative_path)
        module = Module(
            project_id=project.id,
            source_file_id=source_file.id,
            name=identity.name,
            full_name=identity.full_name,
            module_kind=identity.module_kind,
            owner_kind=identity.owner_kind,
            owner_name=identity.owner_name,
            directives_json=json.dumps(parsed.directives, ensure_ascii=False),
        )
        self.session.add(module)
        self.session.flush()
        symbol_ids: dict[int, int] = {}

        def symbol_id_for(ordinal: int | None) -> int | None:
            return symbol_ids.get(ordinal) if ordinal is not None else None

        for parsed_symbol in parsed.symbols:
            symbol = Symbol(
                project_id=project.id,
                module_id=module.id,
                source_file_id=source_file.id,
                name=parsed_symbol.name,
                normalized_name=normalize(parsed_symbol.name),
                kind=parsed_symbol.kind,
                is_export=parsed_symbol.is_export,
                directive=parsed_symbol.directive,
                region=parsed_symbol.region,
                line_start=parsed_symbol.line_start,
                line_end=parsed_symbol.line_end,
                signature=parsed_symbol.signature,
                parameters_json=parsed_symbol.parameters_json(),
                body_hash=parsed_symbol.body_hash,
            )
            self.session.add(symbol)
            self.session.flush()
            symbol_ids[parsed_symbol.ordinal] = symbol.id
        for parsed_call in parsed.calls:
            self.session.add(
                CallSite(
                    project_id=project.id,
                    source_file_id=source_file.id,
                    caller_symbol_id=symbol_id_for(parsed_call.caller_ordinal),
                    callee_name=parsed_call.name,
                    normalized_name=normalize(parsed_call.name),
                    qualifier=parsed_call.qualifier,
                    full_name=parsed_call.full_name,
                    line=parsed_call.line,
                    column=parsed_call.column,
                    resolution="unresolved",
                )
            )
        for parsed_query in parsed.queries:
            self.session.add(
                QueryFact(
                    project_id=project.id,
                    source_file_id=source_file.id,
                    symbol_id=symbol_id_for(parsed_query.caller_ordinal),
                    line_start=parsed_query.line_start,
                    line_end=parsed_query.line_end,
                    kind=parsed_query.kind,
                    text=parsed_query.text,
                    text_hash=sha256(parsed_query.text.encode("utf-8")).hexdigest(),
                    tables_json=json.dumps(
                        [
                            {
                                "name": table.name,
                                "normalized": table.normalized,
                                "operation": table.operation,
                            }
                            for table in parsed_query.tables
                        ],
                        ensure_ascii=False,
                    ),
                )
            )
        for parsed_reference in parsed.references:
            self.session.add(
                Reference(
                    project_id=project.id,
                    source_file_id=source_file.id,
                    symbol_id=symbol_id_for(parsed_reference.caller_ordinal),
                    target_kind=parsed_reference.target_kind,
                    target_name=parsed_reference.target_name,
                    target_full_name=parsed_reference.target_full_name,
                    normalized_target=normalize(parsed_reference.target_full_name),
                    relation=parsed_reference.relation,
                    line=parsed_reference.line,
                    raw_value=parsed_reference.raw_value,
                )
            )
        self.session.flush()

    def _store_metadata(
        self, project: Project, source_file: SourceFile, text: str
    ) -> dict[str, str]:
        parsed = self.metadata.parse(text, source_file.relative_path)
        for metadata_item in parsed.objects:
            self.session.add(
                MetadataObject(
                    project_id=project.id,
                    source_file_id=source_file.id,
                    uuid=metadata_item.uuid,
                    kind=metadata_item.kind,
                    name=metadata_item.name,
                    full_name=metadata_item.full_name,
                    parent_full_name=metadata_item.parent_full_name,
                    synonym=metadata_item.synonym,
                    properties_json=metadata_item.properties_json,
                )
            )
        self.session.flush()
        for metadata_reference in parsed.references:
            self.session.add(
                Reference(
                    project_id=project.id,
                    source_file_id=source_file.id,
                    source_metadata_name=metadata_reference.source_full_name,
                    target_kind=metadata_reference.target_kind,
                    target_name=metadata_reference.target_name,
                    target_full_name=metadata_reference.target_full_name,
                    normalized_target=normalize(metadata_reference.target_full_name),
                    relation="metadata_type_reference",
                    raw_value=metadata_reference.raw_value,
                )
            )
        self.session.flush()
        return parsed.profile

    def _resolve(self, project: Project) -> None:
        self.session.execute(delete(Dependency).where(Dependency.project_id == project.id))
        metadata = list(
            self.session.scalars(
                select(MetadataObject).where(MetadataObject.project_id == project.id)
            )
        )
        modules = list(self.session.scalars(select(Module).where(Module.project_id == project.id)))
        symbols = list(self.session.scalars(select(Symbol).where(Symbol.project_id == project.id)))
        aliases = self._metadata_aliases(metadata)
        metadata_by_kind_name = {(item.kind, normalize(item.name)): item for item in metadata}
        modules_by_id = {item.id: item for item in modules}
        modules_by_file = {item.source_file_id: item for item in modules}

        for module in modules:
            owner = (
                metadata_by_kind_name.get((module.owner_kind, normalize(module.owner_name or "")))
                if module.owner_kind
                else None
            )
            if owner:
                module.metadata_object_id = owner.id
                self._edge(
                    project.id,
                    module.source_file_id,
                    "metadata",
                    owner.id,
                    owner.full_name,
                    "module",
                    module.id,
                    module.full_name,
                    "contains_module",
                    None,
                    True,
                )

        local_symbols: dict[tuple[int, str], list[Symbol]] = {}
        exported: dict[str, list[Symbol]] = {}
        qualified: dict[tuple[str, str], list[Symbol]] = {}
        for symbol in symbols:
            local_symbols.setdefault((symbol.module_id, symbol.normalized_name), []).append(symbol)
            if symbol.is_export:
                exported.setdefault(symbol.normalized_name, []).append(symbol)
            module = modules_by_id[symbol.module_id]
            for qualifier in {module.name, module.owner_name}:
                if qualifier:
                    qualified.setdefault((normalize(qualifier), symbol.normalized_name), []).append(
                        symbol
                    )
        symbols_by_id = {item.id: item for item in symbols}

        calls = list(
            self.session.scalars(select(CallSite).where(CallSite.project_id == project.id))
        )
        for call in calls:
            call.resolved_symbol_id = None
            call.resolution = "unresolved"
            caller = symbols_by_id.get(call.caller_symbol_id) if call.caller_symbol_id else None
            candidates: list[Symbol] = []
            if call.qualifier:
                candidates = qualified.get(
                    (normalize(call.qualifier.split(".")[0]), call.normalized_name), []
                )
                if len(candidates) == 1:
                    call.resolution = "qualified"
            elif caller:
                candidates = local_symbols.get((caller.module_id, call.normalized_name), [])
                if len(candidates) == 1:
                    call.resolution = "same_module"
                elif not candidates:
                    candidates = exported.get(call.normalized_name, [])
                    if len(candidates) == 1:
                        call.resolution = "unique_export"
            if len(candidates) > 1:
                call.resolution = "ambiguous"
            elif len(candidates) == 1:
                target_symbol = candidates[0]
                call.resolved_symbol_id = target_symbol.id
                caller_module = modules_by_file.get(call.source_file_id)
                source_name = (
                    self._symbol_name(caller, modules_by_id)
                    if caller
                    else caller_module.full_name
                    if caller_module
                    else "<module>"
                )
                self._edge(
                    project.id,
                    call.source_file_id,
                    "symbol" if caller else "module",
                    caller.id if caller else None,
                    source_name,
                    "symbol",
                    target_symbol.id,
                    self._symbol_name(target_symbol, modules_by_id),
                    "calls",
                    call.line,
                    True,
                )
            elif call.normalized_name in _BUILTINS:
                call.resolution = "built_in"

        for reference in self.session.scalars(
            select(Reference).where(Reference.project_id == project.id)
        ):
            target_metadata = self._resolve_metadata(reference.target_full_name, aliases)
            reference.metadata_object_id = target_metadata.id if target_metadata else None
            source_symbol = symbols_by_id.get(reference.symbol_id) if reference.symbol_id else None
            source_module = modules_by_file.get(reference.source_file_id)
            source_name = reference.source_metadata_name or (
                self._symbol_name(source_symbol, modules_by_id)
                if source_symbol
                else source_module.full_name
                if source_module
                else "<source>"
            )
            self._edge(
                project.id,
                reference.source_file_id,
                "metadata"
                if reference.source_metadata_name
                else "symbol"
                if source_symbol
                else "module",
                source_symbol.id if source_symbol else None,
                source_name,
                "metadata",
                target_metadata.id if target_metadata else None,
                target_metadata.full_name if target_metadata else reference.target_full_name,
                reference.relation,
                reference.line,
                target_metadata is not None,
                {"raw": reference.raw_value} if reference.raw_value else None,
            )

        for query in self.session.scalars(
            select(QueryFact).where(QueryFact.project_id == project.id)
        ):
            source_symbol = symbols_by_id.get(query.symbol_id) if query.symbol_id else None
            source_name = (
                self._symbol_name(source_symbol, modules_by_id)
                if source_symbol
                else f"query:{query.id}"
            )
            for table in json.loads(query.tables_json):
                target_metadata = self._resolve_metadata(str(table["name"]), aliases)
                self._edge(
                    project.id,
                    query.source_file_id,
                    "symbol" if source_symbol else "query",
                    source_symbol.id if source_symbol else query.id,
                    source_name,
                    "metadata",
                    target_metadata.id if target_metadata else None,
                    target_metadata.full_name if target_metadata else str(table["name"]),
                    f"query_{table['operation']}",
                    query.line_start,
                    target_metadata is not None,
                )

        for item in metadata:
            if item.parent_full_name:
                parent = self._resolve_metadata(item.parent_full_name, aliases)
                if parent:
                    self._edge(
                        project.id,
                        item.source_file_id,
                        "metadata",
                        parent.id,
                        parent.full_name,
                        "metadata",
                        item.id,
                        item.full_name,
                        "contains",
                        None,
                        True,
                    )
        self.session.flush()

    @staticmethod
    def _metadata_aliases(items: list[MetadataObject]) -> dict[str, MetadataObject]:
        aliases: dict[str, MetadataObject] = {}
        for item in items:
            aliases.setdefault(normalize(item.full_name), item)
            aliases.setdefault(normalize(full_metadata_name(item.kind, item.name)), item)
        return aliases

    @staticmethod
    def _resolve_metadata(raw: str, aliases: dict[str, MetadataObject]) -> MetadataObject | None:
        value = normalize(raw)
        if value in aliases:
            return aliases[value]
        matches = [
            (key, item) for key, item in aliases.items() if len(key) > 4 and value.startswith(key)
        ]
        return max(matches, key=lambda pair: len(pair[0]))[1] if matches else None

    def _edge(
        self,
        project_id: int,
        source_file_id: int | None,
        source_kind: str,
        source_id: int | None,
        source_name: str,
        target_kind: str,
        target_id: int | None,
        target_name: str,
        relation: str,
        line: int | None,
        resolved: bool,
        details: dict[str, object] | None = None,
    ) -> None:
        self.session.add(
            Dependency(
                project_id=project_id,
                source_file_id=source_file_id,
                source_kind=source_kind,
                source_id=source_id,
                source_name=source_name,
                target_kind=target_kind,
                target_id=target_id,
                target_name=target_name,
                relation=relation,
                line=line,
                is_resolved=resolved,
                details_json=json.dumps(details, ensure_ascii=False) if details else None,
            )
        )

    @staticmethod
    def _symbol_name(symbol: Symbol, modules: dict[int, Module]) -> str:
        return f"{modules[symbol.module_id].full_name}.{symbol.name}"

    @staticmethod
    def _profile(project: Project) -> dict[str, str]:
        try:
            return {
                str(key): str(value)
                for key, value in json.loads(project.profile_json or "{}").items()
            }
        except (TypeError, ValueError):
            return {}

    def _counts(self, project_id: int) -> dict[str, int]:
        models: dict[str, type[Any]] = {
            "metadata_objects": MetadataObject,
            "modules": Module,
            "symbols": Symbol,
            "calls": CallSite,
            "queries": QueryFact,
            "dependencies": Dependency,
        }
        return {
            name: int(
                self.session.scalar(
                    select(func.count(model.id)).where(model.project_id == project_id)
                )
                or 0
            )
            for name, model in models.items()
        }
