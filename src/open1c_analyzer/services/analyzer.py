"""End-to-end static indexing and dependency-graph construction."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from hashlib import sha256
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import Row, bindparam, delete, func, insert, select
from sqlalchemy.orm import Session

from open1c_analyzer.parser import BslAnalyzer, MetadataParser, identify_module, read_text
from open1c_analyzer.parser.names import MANAGER_KIND, full_metadata_name, normalize
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
        "Булево",
        "Вопрос",
        "Вычислить",
        "Дата",
        "ДобавитьМесяц",
        "ЗаполнитьЗначенияСвойств",
        "ЗначениеВСтрокуВнутр",
        "ЗначениеЗаполнено",
        "ЗначениеИзСтрокиВнутр",
        "Макс",
        "Мин",
        "НачалоГода",
        "НачалоДня",
        "НачалоКвартала",
        "НачалоМесяца",
        "НачалоНедели",
        "НСтр",
        "ОписаниеОшибки",
        "Окр",
        "Предупреждение",
        "ПустаяСтрока",
        "СокрЛ",
        "СокрЛП",
        "СокрП",
        "Сообщить",
        "Сред",
        "СтрДлина",
        "СтрЗаменить",
        "СтрНайти",
        "СтрПолучитьСтроку",
        "СтрРазделить",
        "СтрСоединить",
        "ТекущаяДата",
        "ТекущаяДатаСеанса",
        "Тип",
        "ТипЗнч",
        "Формат",
        "Число",
        "ВРег",
        "ИнформацияОбОшибке",
        "Лев",
        "НРег",
        "ПредопределенноеЗначение",
        "СтрНачинаетсяС",
        "СтрЧислоВхождений",
        "СтрЗаканчиваетсяНа",
        "СтрСравнить",
        "СтрШаблон",
        "Строка",
        "КонецГода",
        "КонецДня",
        "КонецКвартала",
        "КонецМесяца",
        "КонецНедели",
        "Год",
        "Месяц",
        "День",
        "ДеньНедели",
        "ДеньГода",
        "НеделяГода",
        "Час",
        "Минута",
        "Секунда",
        "Символ",
        "Base64Строка",
        "Прав",
        "Найти",
        "Цел",
        "String",
        "Number",
        "Type",
    )
}

_PLATFORM_GLOBALS = {
    normalize(item)
    for item in (
        "ВыполнитьОбработкуОповещения",
        "ЗафиксироватьТранзакцию",
        "ЗаписьЖурналаРегистрации",
        "Запрос",
        "Массив",
        "НачатьТранзакцию",
        "ОписаниеОповещения",
        "ОписаниеТипов",
        "ОткрытьФорму",
        "ОтменитьТранзакцию",
        "ПоказатьВопрос",
        "ПоказатьПредупреждение",
        "ПодключитьОбработчикОжидания",
        "ПолеКомпоновкиДанных",
        "ПолучитьИзВременногоХранилища",
        "ПолучитьФункциональнуюОпцию",
        "ПоместитьВоВременноеХранилище",
        "Структура",
        "УстановитьПривилегированныйРежим",
        "Оповестить",
        "ОчиститьСообщения",
        "ПравоДоступа",
        "РеквизитФормыВЗначение",
        "ЗначениеВРеквизитФормы",
        "ПолучитьМакет",
        "XMLСтрока",
        "ПоказатьЗначение",
        "ПолучитьИмяВременногоФайла",
        "ПодробноеПредставлениеОшибки",
        "ПоказатьОповещениеПользователя",
        "КвалификаторыСтроки",
        "КвалификаторыЧисла",
        "КвалификаторыДаты",
        "ФиксированнаяСтруктура",
        "ХранилищеЗначения",
        "Соответствие",
        "ФиксированноеСоответствие",
        "Цвет",
        "ФиксированныйМассив",
        "ЭтоАдресВременногоХранилища",
        "ЗначениеВДанныеФормы",
        "КопироватьДанныеФормы",
        "ДанныеФормыВЗначение",
        "УдалитьФайлы",
        "Состояние",
        "ПолучитьНавигационнуюСсылку",
        "Файл",
        "ПолучитьФорму",
        "ДвоичныеДанные",
        "УдалитьИзВременногоХранилища",
        "ПредставлениеПериода",
        "Шрифт",
        "ПараметрВыбора",
        "УстановитьОтключениеБезопасногоРежима",
        "СжатиеДанных",
        "ТекущаяУниверсальнаяДата",
        "Линия",
        "СоздатьНаборЗаписей",
        "ПустаяСсылка",
        "ОповеститьОбИзменении",
        "ОповеститьОВыборе",
        "ТекущаяУниверсальнаяДатаВМиллисекундах",
    )
}

# False call rows created by older parser versions can remain in an existing index.
# Remove them during graph resolution so a full source reindex is not required.
_PARSER_NOISE = {
    normalize(item)
    for item in (
        "and",
        "do",
        "else",
        "elseif",
        "for",
        "function",
        "if",
        "new",
        "not",
        "or",
        "procedure",
        "return",
        "then",
        "to",
        "try",
        "while",
        "возврат",
        "вызватьисключение",
        "вместо",
        "для",
        "если",
        "и",
        "изменениеиконтроль",
        "иначе",
        "иначеесли",
        "или",
        "не",
        "новый",
        "перед",
        "после",
        "по",
        "пока",
        "попытка",
        "процедура",
        "тогда",
        "функция",
        "цикл",
    )
}

_PLATFORM_QUALIFIERS = {
    normalize(item)
    for item in (
        "ЭтотОбъект",
        "ThisObject",
        "Объект",
        "Object",
        "Элементы",
        "Elements",
        "Параметры",
        "Parameters",
        "Команды",
        "Commands",
        "ПользователиИнформационнойБазы",
        "РегламентныеЗадания",
        "ФоновыеЗадания",
        "Метаданные",
        "Metadata",
    )
}
_MANAGER_QUALIFIERS = {normalize(item) for item in MANAGER_KIND}
_ANALYSIS_SCHEMA_VERSION = "3"
_ANALYSIS_SCHEMA_KEY = "Open1CAnalysisSchemaVersion"


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


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    calls: int
    resolved: int
    ambiguous: int
    built_in: int
    platform: int
    dynamic: int
    unresolved: int
    dependencies: int


@dataclass(frozen=True, slots=True)
class AnalysisProgress:
    stage: str
    completed: int
    total: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _Symbol:
    id: int
    project_id: int
    module_id: int
    name: str
    normalized_name: str
    is_export: bool


_BATCH_SIZE = 1000


class AnalyzerCore:
    """Build the facts required for later task analysis and LLM context selection."""

    def __init__(
        self, session: Session, *, progress: Callable[[AnalysisProgress], None] | None = None
    ) -> None:
        self.session = session
        self.catalog = ProjectCatalog(session)
        self.bsl = BslAnalyzer()
        self.metadata = MetadataParser()
        self.progress = progress
        self._edges: list[dict[str, Any]] = []

    def _emit(self, stage: str, completed: int, total: int, detail: str = "") -> None:
        if self.progress is not None:
            self.progress(AnalysisProgress(stage, completed, total, detail))

    def _insert_rows(self, model: type[Any], rows: Iterable[dict[str, Any]]) -> None:
        iterator = iter(rows)
        while batch := list(islice(iterator, _BATCH_SIZE)):
            self.session.connection().execute(model.__table__.insert(), batch)

    def _batches(self, model: type[Any], project_id: int) -> Iterator[list[Row[Any]]]:
        table = model.__table__
        last_id = 0
        while rows := list(
            self.session.connection().execute(
                select(table)
                .where(table.c.project_id == project_id, table.c.id > last_id)
                .order_by(table.c.id)
                .limit(_BATCH_SIZE)
            )
        ):
            last_id = rows[-1].id
            yield rows

    def _update_rows(self, model: type[Any], rows: list[dict[str, Any]]) -> None:
        if rows:
            table = model.__table__
            self.session.connection().execute(
                table.update().where(table.c.id == bindparam("_key")), rows
            )

    def _symbols(self, project_ids: tuple[int, ...]) -> list[_Symbol]:
        return [
            _Symbol(*row)
            for row in self.session.execute(
                select(
                    Symbol.id,
                    Symbol.project_id,
                    Symbol.module_id,
                    Symbol.name,
                    Symbol.normalized_name,
                    Symbol.is_export,
                ).where(Symbol.project_id.in_(project_ids))
            )
        ]

    def _flush_edges(self) -> None:
        self._insert_rows(Dependency, self._edges)
        self._edges.clear()

    def analyze_project(
        self,
        name: str,
        *,
        force: bool = False,
        scan: bool = True,
        include: tuple[str, ...] = (),
    ) -> AnalysisResult:
        project = self.catalog.get_project(name)
        if not project.source_path:
            raise SourceDirectoryError(f"Project has no source directory: {name}")
        root = Path(project.source_path)
        self._emit("scan", 0, 0, "Discovering source files" if scan else "Using file catalog")
        scan_result = (
            self.catalog.scan_project(
                name, progress=lambda done, total: self._emit("scan", done, total)
            )
            if scan
            else None
        )
        analyzed = 0
        skipped = 0
        errors: list[str] = []
        profile = self._profile(project)
        parser_upgrade = profile.get(_ANALYSIS_SCHEMA_KEY) != _ANALYSIS_SCHEMA_VERSION
        reanalyze_all = force or parser_upgrade

        files = [item for item in self.catalog.list_files(project) if self._analyzable(item)]
        self._emit("files", 0, len(files))
        for position, source_file in enumerate(files, 1):
            if (
                not reanalyze_all
                and source_file.analyzed_checksum == source_file.checksum
                and not source_file.analysis_error
            ):
                skipped += 1
                self._emit("files", position, len(files), source_file.relative_path)
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
            self._emit("files", position, len(files), relative_path)

        profile[_ANALYSIS_SCHEMA_KEY] = _ANALYSIS_SCHEMA_VERSION
        project.profile_json = (
            json.dumps(profile, ensure_ascii=False, sort_keys=True) if profile else None
        )
        graph_rebuilt = (
            reanalyze_all
            or analyzed > 0
            or bool(scan_result and scan_result.removed)
            or bool(include)
        )
        if graph_rebuilt:
            self._resolve(project, self._included_projects(project, include))
        project.last_analyzed_at = datetime.now(UTC)
        self.session.flush()
        counts = self._counts(project.id)
        unresolved = int(
            self.session.scalar(
                select(func.count(CallSite.id)).where(
                    CallSite.project_id == project.id, CallSite.resolution.like("unresolved%")
                )
            )
            or 0
        )
        self._emit("ready", len(files), len(files), "Waiting for transaction commit")
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

    def resolve_project(
        self,
        name: str,
        *,
        include: tuple[str, ...] = (),
    ) -> ResolutionResult:
        """Rebuild call resolution, optionally using symbols from related projects."""
        project = self.catalog.get_project(name)
        self._resolve(project, self._included_projects(project, include))
        project.last_analyzed_at = datetime.now(UTC)
        self.session.flush()
        return ResolutionResult(
            calls=self._call_count(project.id),
            resolved=self._call_count(project.id, CallSite.resolved_symbol_id.is_not(None)),
            ambiguous=self._call_count(project.id, CallSite.resolution.like("ambiguous%")),
            built_in=self._call_count(project.id, CallSite.resolution == "built_in"),
            platform=self._call_count(project.id, CallSite.resolution == "platform_api"),
            dynamic=self._call_count(project.id, CallSite.resolution == "dynamic_qualified"),
            unresolved=self._call_count(project.id, CallSite.resolution.like("unresolved%")),
            dependencies=int(
                self.session.scalar(
                    select(func.count(Dependency.id)).where(Dependency.project_id == project.id)
                )
                or 0
            ),
        )

    def _included_projects(
        self,
        primary: Project,
        names: tuple[str, ...],
    ) -> tuple[Project, ...]:
        result: list[Project] = []
        seen = {primary.id}
        for name in names:
            project = self.catalog.get_project(name)
            if project.id in seen:
                continue
            result.append(project)
            seen.add(project.id)
        return tuple(result)

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
        parts = path.parts
        if (
            path.name.casefold() == "form.xml"
            and "Forms" in parts
            and parts.index("Forms") + 2 < len(parts)
        ):
            return True
        return len(path.parts) == 2 and path.suffix.casefold() == ".xml"

    def _clear_file(self, file_id: int) -> None:
        # Python predicate evaluation visits the entire Session identity map for each
        # DELETE, including every catalogued SourceFile. RETURNING synchronizes only
        # the affected identities and still marks cached deleted facts as deleted.
        for model in (Reference, CallSite, QueryFact, Symbol, Module, MetadataObject):
            self.session.execute(
                delete(model)
                .where(model.source_file_id == file_id)
                .execution_options(synchronize_session="fetch")
            )
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
        symbol_rows = [
            dict(
                project_id=project.id,
                module_id=module.id,
                source_file_id=source_file.id,
                name=item.name,
                normalized_name=normalize(item.name),
                kind=item.kind,
                is_export=item.is_export,
                directive=item.directive,
                region=item.region,
                line_start=item.line_start,
                line_end=item.line_end,
                signature=item.signature,
                parameters_json=item.parameters_json(),
                body_hash=item.body_hash,
            )
            for item in parsed.symbols
        ]
        # Explicit ordered RETURNING keeps caller ordinals correct even for declarations
        # on the same line. Never assume SQLite's unordered RETURNING follows input order.
        ids = (
            list(
                self.session.scalars(
                    insert(Symbol).returning(Symbol.id, sort_by_parameter_order=True), symbol_rows
                )
            )
            if symbol_rows
            else []
        )
        symbol_ids = {
            item.ordinal: identifier for item, identifier in zip(parsed.symbols, ids, strict=True)
        }

        def symbol_id_for(ordinal: int | None) -> int | None:
            return symbol_ids.get(ordinal) if ordinal is not None else None

        self._insert_rows(
            CallSite,
            (
                dict(
                    project_id=project.id,
                    source_file_id=source_file.id,
                    caller_symbol_id=symbol_id_for(item.caller_ordinal),
                    callee_name=item.name,
                    normalized_name=normalize(item.name),
                    qualifier=item.qualifier,
                    full_name=item.full_name,
                    line=item.line,
                    column=item.column,
                    resolution="unresolved",
                )
                for item in parsed.calls
            ),
        )
        self._insert_rows(
            QueryFact,
            (
                dict(
                    project_id=project.id,
                    source_file_id=source_file.id,
                    symbol_id=symbol_id_for(item.caller_ordinal),
                    line_start=item.line_start,
                    line_end=item.line_end,
                    kind=item.kind,
                    text=item.text,
                    text_hash=sha256(item.text.encode("utf-8")).hexdigest(),
                    tables_json=json.dumps(
                        [
                            dict(
                                name=table.name,
                                normalized=table.normalized,
                                operation=table.operation,
                            )
                            for table in item.tables
                        ],
                        ensure_ascii=False,
                    ),
                )
                for item in parsed.queries
            ),
        )
        self._insert_rows(
            Reference,
            (
                dict(
                    project_id=project.id,
                    source_file_id=source_file.id,
                    symbol_id=symbol_id_for(item.caller_ordinal),
                    target_kind=item.target_kind,
                    target_name=item.target_name,
                    target_full_name=item.target_full_name,
                    normalized_target=normalize(item.target_full_name),
                    relation=item.relation,
                    line=item.line,
                    raw_value=item.raw_value,
                )
                for item in parsed.references
            ),
        )

    def _store_metadata(
        self, project: Project, source_file: SourceFile, text: str
    ) -> dict[str, str]:
        parsed = self.metadata.parse(text, source_file.relative_path)
        self._insert_rows(
            MetadataObject,
            (
                dict(
                    project_id=project.id,
                    source_file_id=source_file.id,
                    uuid=item.uuid,
                    kind=item.kind,
                    name=item.name,
                    full_name=item.full_name,
                    parent_full_name=item.parent_full_name,
                    synonym=item.synonym,
                    properties_json=item.properties_json,
                )
                for item in parsed.objects
            ),
        )
        self._insert_rows(
            Reference,
            (
                dict(
                    project_id=project.id,
                    source_file_id=source_file.id,
                    source_metadata_name=item.source_full_name,
                    target_kind=item.target_kind,
                    target_name=item.target_name,
                    target_full_name=item.target_full_name,
                    normalized_target=normalize(item.target_full_name),
                    relation="metadata_type_reference",
                    raw_value=item.raw_value,
                )
                for item in parsed.references
            ),
        )
        return parsed.profile

    def _resolve(
        self,
        project: Project,
        included_projects: tuple[Project, ...] = (),
    ) -> None:
        self._edges.clear()
        self._emit("resolve", 0, 0, "Loading metadata and symbol indexes")
        self.session.execute(delete(Dependency).where(Dependency.project_id == project.id))
        self.session.execute(
            delete(CallSite).where(
                CallSite.project_id == project.id,
                CallSite.normalized_name.in_(_PARSER_NOISE),
            )
        )

        primary_metadata = list(
            self.session.scalars(
                select(MetadataObject).where(MetadataObject.project_id == project.id)
            )
        )
        primary_modules = list(
            self.session.scalars(select(Module).where(Module.project_id == project.id))
        )
        primary_symbols = self._symbols((project.id,))
        included_ids = tuple(item.id for item in included_projects)
        included_metadata = (
            list(
                self.session.scalars(
                    select(MetadataObject).where(MetadataObject.project_id.in_(included_ids))
                )
            )
            if included_ids
            else []
        )
        included_modules = (
            list(self.session.scalars(select(Module).where(Module.project_id.in_(included_ids))))
            if included_ids
            else []
        )
        included_symbols = self._symbols(included_ids) if included_ids else []

        all_metadata = [*primary_metadata, *included_metadata]
        all_modules = [*primary_modules, *included_modules]
        aliases = self._metadata_aliases(all_metadata)

        @lru_cache(maxsize=65_536)
        def resolve_metadata(raw: str) -> MetadataObject | None:
            return self._resolve_metadata(raw, aliases)

        registers: dict[str, list[MetadataObject]] = {}
        for item in all_metadata:
            if item.kind in {
                "accounting_register",
                "accumulation_register",
                "calculation_register",
                "information_register",
            }:
                registers.setdefault(normalize(item.name), []).append(item)

        @lru_cache(maxsize=4096)
        def resolve_register(raw: str) -> MetadataObject | None:
            candidates = registers.get(normalize(raw), [])
            primary = [item for item in candidates if item.project_id == project.id]
            if len(primary) == 1:
                return primary[0]
            return candidates[0] if len(candidates) == 1 else None

        metadata_by_kind_name: dict[tuple[str, str], MetadataObject] = {}
        for item in all_metadata:
            metadata_by_kind_name.setdefault((item.kind, normalize(item.name)), item)
        modules_by_id = {item.id: item for item in all_modules}
        modules_by_file = {item.source_file_id: item for item in primary_modules}
        module_qualifiers = {normalize(item.name) for item in all_modules}

        for module in primary_modules:
            owner_kind = module.owner_kind
            if module.module_kind == "form_module" and ".Form." in module.full_name:
                form_full_name = module.full_name.rsplit(".form_module", 1)[0]
                owner = aliases.get(normalize(form_full_name))
            else:
                owner = (
                    metadata_by_kind_name.get((owner_kind, normalize(module.owner_name or "")))
                    if owner_kind
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

        local_symbols: dict[tuple[int, str], list[_Symbol]] = {}
        primary_exported: dict[str, list[_Symbol]] = {}
        included_exported: dict[str, list[_Symbol]] = {}
        primary_qualified: dict[tuple[str, str], list[_Symbol]] = {}
        included_qualified: dict[tuple[str, str], list[_Symbol]] = {}

        for symbol in primary_symbols:
            local_symbols.setdefault((symbol.module_id, symbol.normalized_name), []).append(symbol)
            if symbol.is_export:
                primary_exported.setdefault(symbol.normalized_name, []).append(symbol)
            module = modules_by_id[symbol.module_id]
            for qualifier in {module.name, module.owner_name}:
                if qualifier:
                    primary_qualified.setdefault(
                        (normalize(qualifier), symbol.normalized_name), []
                    ).append(symbol)

        for symbol in included_symbols:
            if not symbol.is_export:
                continue
            included_exported.setdefault(symbol.normalized_name, []).append(symbol)
            module = modules_by_id[symbol.module_id]
            for qualifier in {module.name, module.owner_name}:
                if qualifier:
                    included_qualified.setdefault(
                        (normalize(qualifier), symbol.normalized_name), []
                    ).append(symbol)

        document_managers: dict[tuple[str, str], list[_Symbol]] = {}
        included_document_managers: dict[tuple[str, str], list[_Symbol]] = {}
        for pool, index in (
            (primary_symbols, document_managers),
            (included_symbols, included_document_managers),
        ):
            for symbol in pool:
                module = modules_by_id[symbol.module_id]
                if (
                    symbol.is_export
                    and module.module_kind == "manager_module"
                    and module.owner_kind == "document"
                ):
                    key = (normalize(module.owner_name or ""), symbol.normalized_name)
                    index.setdefault(key, []).append(symbol)

        symbols_by_id = {item.id: item for item in primary_symbols}
        self.session.flush()
        self._flush_edges()
        call_total = self._call_count(project.id)
        completed = 0
        self._emit("calls", 0, call_total)
        for batch in self._batches(CallSite, project.id):
            updates: list[dict[str, Any]] = []
            for call in batch:
                resolved_symbol_id: int | None = None
                resolution = "unresolved_unqualified"
                caller = symbols_by_id.get(call.caller_symbol_id) if call.caller_symbol_id else None
                candidates: list[_Symbol] = []
                qualifier_root = normalize(call.qualifier.split(".")[0]) if call.qualifier else ""
                qualifier_parts = call.qualifier.split(".") if call.qualifier else []
                if len(qualifier_parts) == 2 and qualifier_root in {
                    normalize("Документы"),
                    normalize("Documents"),
                }:
                    # Only exported members of this document's manager are callable here.
                    # Never fall back to a same-named object/form/common-module method.
                    document_name = normalize(qualifier_parts[1])
                    for manager_index, resolution in (
                        (document_managers, "document_manager"),
                        (included_document_managers, "included_document_manager"),
                    ):
                        candidates = manager_index.get((document_name, call.normalized_name), [])
                        if candidates:
                            resolution = (
                                resolution if len(candidates) == 1 else "ambiguous_document_manager"
                            )
                            break
                    if not candidates:
                        resolution = "platform_api"
                elif call.qualifier:
                    if caller and qualifier_root in {
                        normalize("ЭтотОбъект"),
                        normalize("ThisObject"),
                    }:
                        candidates = local_symbols.get((caller.module_id, call.normalized_name), [])
                        if len(candidates) == 1:
                            resolution = "same_module_object"
                    if not candidates:
                        candidates = primary_qualified.get(
                            (qualifier_root, call.normalized_name), []
                        )
                        if len(candidates) == 1:
                            resolution = "qualified_module"
                    if not candidates:
                        candidates = included_qualified.get(
                            (qualifier_root, call.normalized_name), []
                        )
                        if len(candidates) == 1:
                            resolution = "included_qualified_module"
                        elif len(candidates) > 1:
                            resolution = "ambiguous_included_qualified"
                    elif len(candidates) > 1:
                        resolution = "ambiguous_qualified"
                    if not candidates:
                        if (
                            qualifier_root in _PLATFORM_QUALIFIERS
                            or qualifier_root in _MANAGER_QUALIFIERS
                        ):
                            resolution = "platform_api"
                        elif qualifier_root in module_qualifiers:
                            resolution = "unresolved_qualified"
                        else:
                            resolution = "dynamic_qualified"
                elif caller:
                    candidates = local_symbols.get((caller.module_id, call.normalized_name), [])
                    if len(candidates) == 1:
                        resolution = "same_module"
                    elif len(candidates) > 1:
                        resolution = "ambiguous_local"
                    else:
                        common_candidates = [
                            item
                            for item in primary_exported.get(call.normalized_name, [])
                            if modules_by_id[item.module_id].owner_kind == "common_module"
                        ]
                        if len(common_candidates) == 1:
                            candidates = common_candidates
                            resolution = "global_common_module"
                        else:
                            candidates = primary_exported.get(call.normalized_name, [])
                            if len(candidates) == 1:
                                resolution = "unique_export"
                            elif len(candidates) > 1:
                                resolution = "ambiguous_export"
                        if not candidates:
                            included_common = [
                                item
                                for item in included_exported.get(call.normalized_name, [])
                                if modules_by_id[item.module_id].owner_kind == "common_module"
                            ]
                            if len(included_common) == 1:
                                candidates = included_common
                                resolution = "included_global_common_module"
                            elif len(included_common) > 1:
                                candidates = included_common
                                resolution = "ambiguous_included_common_module"
                            else:
                                candidates = included_exported.get(call.normalized_name, [])
                                if len(candidates) == 1:
                                    resolution = "included_unique_export"
                                elif len(candidates) > 1:
                                    resolution = "ambiguous_included_export"
                if len(candidates) == 1:
                    target_symbol = candidates[0]
                    resolved_symbol_id = target_symbol.id
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
                        {"target_project": target_symbol.project_id}
                        if target_symbol.project_id != project.id
                        else None,
                    )
                elif not call.qualifier and call.normalized_name in _BUILTINS:
                    resolution = "built_in"
                elif not call.qualifier and call.normalized_name in _PLATFORM_GLOBALS:
                    resolution = "platform_api"
                updates.append(
                    dict(_key=call.id, resolved_symbol_id=resolved_symbol_id, resolution=resolution)
                )
            self._update_rows(CallSite, updates)
            self._flush_edges()
            completed += len(batch)
            self._emit("calls", completed, call_total)

        reference_total = self._row_count(Reference, project.id)
        completed = 0
        self._emit("references", 0, reference_total)
        for batch in self._batches(Reference, project.id):
            updates = []
            for reference in batch:
                target_metadata = (
                    resolve_register(reference.target_name)
                    if reference.target_kind == "register"
                    else resolve_metadata(reference.target_full_name)
                )
                updates.append(
                    dict(
                        _key=reference.id,
                        metadata_object_id=target_metadata.id if target_metadata else None,
                    )
                )
                source_symbol = (
                    symbols_by_id.get(reference.symbol_id) if reference.symbol_id else None
                )
                source_module = modules_by_file.get(reference.source_file_id)
                source_name = reference.source_metadata_name or (
                    self._symbol_name(source_symbol, modules_by_id)
                    if source_symbol
                    else source_module.full_name
                    if source_module
                    else "<source>"
                )
                details: dict[str, object] = (
                    {"raw": reference.raw_value} if reference.raw_value else {}
                )
                if target_metadata and target_metadata.project_id != project.id:
                    details["target_project"] = target_metadata.project_id
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
                    details or None,
                )
            self._update_rows(Reference, updates)
            self._flush_edges()
            completed += len(batch)
            self._emit("references", completed, reference_total)

        query_total = self._row_count(QueryFact, project.id)
        completed = 0
        self._emit("queries", 0, query_total)
        for batch in self._batches(QueryFact, project.id):
            for query in batch:
                source_symbol = symbols_by_id.get(query.symbol_id) if query.symbol_id else None
                source_name = (
                    self._symbol_name(source_symbol, modules_by_id)
                    if source_symbol
                    else f"query:{query.id}"
                )
                for table in json.loads(query.tables_json):
                    target_metadata = resolve_metadata(str(table["name"]))
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
                        {"target_project": target_metadata.project_id}
                        if target_metadata and target_metadata.project_id != project.id
                        else None,
                    )
            self._flush_edges()
            completed += len(batch)
            self._emit("queries", completed, query_total)

        for item in primary_metadata:
            if item.parent_full_name:
                parent = resolve_metadata(item.parent_full_name)
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
                        {"source_project": parent.project_id}
                        if parent.project_id != project.id
                        else None,
                    )
        self._flush_edges()
        self.session.flush()
        # Core bulk updates bypass ORM state; callers reusing this Session must not
        # observe cached pre-resolution call/reference objects.
        self.session.expire_all()

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
        # Preserve longest-prefix matching without scanning configuration metadata.
        # Short aliases remain valid only as exact matches.
        for length in range(len(value) - 1, 4, -1):
            if match := aliases.get(value[:length]):
                return match
        return None

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
        self._edges.append(
            dict(
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
        if len(self._edges) >= _BATCH_SIZE:
            self._flush_edges()

    def _row_count(self, model: type[Any], project_id: int) -> int:
        return int(
            self.session.scalar(select(func.count(model.id)).where(model.project_id == project_id))
            or 0
        )

    @staticmethod
    def _symbol_name(symbol: _Symbol, modules: dict[int, Module]) -> str:
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

    def _call_count(self, project_id: int, *conditions: Any) -> int:
        return int(
            self.session.scalar(
                select(func.count(CallSite.id)).where(
                    CallSite.project_id == project_id, *conditions
                )
            )
            or 0
        )

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
