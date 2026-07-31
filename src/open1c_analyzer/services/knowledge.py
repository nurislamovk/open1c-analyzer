"""Search, reporting and snapshot export over the collected graph."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from open1c_analyzer.parser.names import normalize
from open1c_analyzer.services.project_catalog import ProjectCatalog
from open1c_analyzer.storage.models import (
    CallSite,
    Dependency,
    MetadataObject,
    Module,
    QueryFact,
    SourceFile,
    Symbol,
)


@dataclass(frozen=True, slots=True)
class Summary:
    project: str
    files: int
    metadata_objects: int
    modules: int
    symbols: int
    exported_symbols: int
    calls: int
    resolved_calls: int
    ambiguous_calls: int
    built_in_calls: int
    unresolved_calls: int
    queries: int
    dependencies: int
    unresolved_dependencies: int
    analysis_errors: int


@dataclass(frozen=True, slots=True)
class SearchHit:
    kind: str
    name: str
    location: str
    details: str


@dataclass(frozen=True, slots=True)
class CallEdge:
    caller: str
    callee: str
    resolution: str
    file: str
    line: int


class KnowledgeService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.catalog = ProjectCatalog(session)

    def summary(self, name: str) -> Summary:
        project = self.catalog.get_project(name)
        return Summary(
            project.name,
            self._count(SourceFile, project.id),
            self._count(MetadataObject, project.id),
            self._count(Module, project.id),
            self._count(Symbol, project.id),
            self._count(Symbol, project.id, Symbol.is_export.is_(True)),
            self._count(CallSite, project.id),
            self._count(CallSite, project.id, CallSite.resolved_symbol_id.is_not(None)),
            self._count(CallSite, project.id, CallSite.resolution == "ambiguous"),
            self._count(CallSite, project.id, CallSite.resolution == "built_in"),
            self._count(CallSite, project.id, CallSite.resolution == "unresolved"),
            self._count(QueryFact, project.id),
            self._count(Dependency, project.id),
            self._count(Dependency, project.id, Dependency.is_resolved.is_(False)),
            self._count(SourceFile, project.id, SourceFile.analysis_error.is_not(None)),
        )

    def find(self, name: str, term: str, limit: int = 100) -> list[SearchHit]:
        project = self.catalog.get_project(name)
        pattern = f"%{term.casefold()}%"
        files = self._files(project.id)
        modules = {
            item.id: item
            for item in self.session.scalars(select(Module).where(Module.project_id == project.id))
        }
        hits: list[SearchHit] = []
        metadata = self.session.scalars(
            select(MetadataObject)
            .where(
                MetadataObject.project_id == project.id,
                or_(
                    func.lower(MetadataObject.name).like(pattern),
                    func.lower(MetadataObject.full_name).like(pattern),
                    func.lower(func.coalesce(MetadataObject.synonym, "")).like(pattern),
                ),
            )
            .order_by(MetadataObject.full_name)
            .limit(limit)
        )
        hits.extend(
            SearchHit(
                f"metadata:{item.kind}",
                item.full_name,
                self._file_path(files, item.source_file_id),
                item.synonym or "",
            )
            for item in metadata
        )
        remaining = max(0, limit - len(hits))
        for item in self.session.scalars(
            select(Symbol)
            .where(Symbol.project_id == project.id, func.lower(Symbol.name).like(pattern))
            .order_by(Symbol.name)
            .limit(remaining)
        ):
            module = modules[item.module_id]
            hits.append(
                SearchHit(
                    f"symbol:{item.kind}",
                    f"{module.full_name}.{item.name}",
                    f"{self._file_path(files, item.source_file_id)}:{item.line_start}",
                    (
                        f"export={item.is_export}; "
                        f"directive={item.directive or '-'}; "
                        f"region={item.region or '-'}"
                    ),
                )
            )
        return hits

    def callers(self, name: str, term: str) -> list[CallEdge]:
        return self._calls(name, term, incoming=True)

    def callees(self, name: str, term: str) -> list[CallEdge]:
        return self._calls(name, term, incoming=False)

    def dependencies(
        self, name: str, term: str, incoming: bool = True, limit: int = 200
    ) -> list[Dependency]:
        project = self.catalog.get_project(name)
        column = Dependency.target_name if incoming else Dependency.source_name
        return list(
            self.session.scalars(
                select(Dependency)
                .where(
                    Dependency.project_id == project.id,
                    func.lower(column).like(f"%{term.casefold()}%"),
                )
                .order_by(Dependency.source_name, Dependency.relation, Dependency.target_name)
                .limit(limit)
            )
        )

    def query_usage(self, name: str, table_term: str | None = None) -> list[dict[str, Any]]:
        project = self.catalog.get_project(name)
        files = self._files(project.id)
        symbols = {
            item.id: item.name
            for item in self.session.scalars(select(Symbol).where(Symbol.project_id == project.id))
        }
        result: list[dict[str, Any]] = []
        for query in self.session.scalars(
            select(QueryFact)
            .where(QueryFact.project_id == project.id)
            .order_by(QueryFact.source_file_id, QueryFact.line_start)
        ):
            for table in json.loads(query.tables_json):
                if table_term and normalize(table_term) not in normalize(str(table["name"])):
                    continue
                query_edges = self.session.scalars(
                    select(Dependency).where(
                        Dependency.project_id == project.id,
                        Dependency.source_file_id == query.source_file_id,
                        Dependency.line == query.line_start,
                        Dependency.relation == f"query_{table['operation']}",
                    )
                )
                table_key = normalize(str(table["name"]))
                target = next(
                    (
                        edge
                        for edge in query_edges
                        if table_key.startswith(normalize(edge.target_name))
                        or normalize(edge.target_name).startswith(table_key)
                    ),
                    None,
                )
                result.append(
                    {
                        "file": self._file_path(files, query.source_file_id),
                        "line": query.line_start,
                        "symbol": symbols.get(query.symbol_id)
                        if query.symbol_id is not None
                        else None,
                        "query_kind": query.kind,
                        "table": table["name"],
                        "operation": table["operation"],
                        "resolved": bool(target and target.is_resolved),
                    }
                )
        return result

    def export_snapshot(self, name: str, output: Path) -> Path:
        project = self.catalog.get_project(name)
        files = self._files(project.id)
        modules = list(
            self.session.scalars(
                select(Module).where(Module.project_id == project.id).order_by(Module.full_name)
            )
        )
        module_names = {item.id: item.full_name for item in modules}
        symbols = list(
            self.session.scalars(
                select(Symbol)
                .where(Symbol.project_id == project.id)
                .order_by(Symbol.module_id, Symbol.line_start)
            )
        )
        metadata = list(
            self.session.scalars(
                select(MetadataObject)
                .where(MetadataObject.project_id == project.id)
                .order_by(MetadataObject.full_name)
            )
        )
        dependencies = list(
            self.session.scalars(
                select(Dependency)
                .where(Dependency.project_id == project.id)
                .order_by(Dependency.source_name, Dependency.relation, Dependency.target_name)
            )
        )
        payload = {
            "schema": "open1c-analyzer-snapshot-v1",
            "project": {
                "name": project.name,
                "source_path": project.source_path,
                "profile": json.loads(project.profile_json or "{}"),
                "last_analyzed_at": project.last_analyzed_at.isoformat()
                if project.last_analyzed_at
                else None,
            },
            "summary": asdict(self.summary(name)),
            "metadata": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "name": item.name,
                    "full_name": item.full_name,
                    "parent": item.parent_full_name,
                    "uuid": item.uuid,
                    "synonym": item.synonym,
                    "properties": json.loads(item.properties_json or "{}"),
                    "file": self._optional_file_path(files, item.source_file_id),
                }
                for item in metadata
            ],
            "modules": [
                {
                    "id": item.id,
                    "full_name": item.full_name,
                    "module_kind": item.module_kind,
                    "owner_kind": item.owner_kind,
                    "owner_name": item.owner_name,
                    "metadata_object_id": item.metadata_object_id,
                    "file": self._optional_file_path(files, item.source_file_id),
                }
                for item in modules
            ],
            "symbols": [
                {
                    "id": item.id,
                    "module": module_names[item.module_id],
                    "name": item.name,
                    "kind": item.kind,
                    "export": item.is_export,
                    "directive": item.directive,
                    "region": item.region,
                    "line_start": item.line_start,
                    "line_end": item.line_end,
                    "signature": item.signature,
                    "parameters": json.loads(item.parameters_json),
                    "file": self._optional_file_path(files, item.source_file_id),
                }
                for item in symbols
            ],
            "queries": self.query_usage(name),
            "dependencies": [
                {
                    "source_kind": item.source_kind,
                    "source": item.source_name,
                    "relation": item.relation,
                    "target_kind": item.target_kind,
                    "target": item.target_name,
                    "resolved": item.is_resolved,
                    "file": self._optional_file_path(files, item.source_file_id),
                    "line": item.line,
                }
                for item in dependencies
            ],
        }
        output = output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        return output

    def _calls(self, name: str, term: str, incoming: bool) -> list[CallEdge]:
        project = self.catalog.get_project(name)
        files = self._files(project.id)
        modules = {
            item.id: item.full_name
            for item in self.session.scalars(select(Module).where(Module.project_id == project.id))
        }
        symbols = {
            item.id: item
            for item in self.session.scalars(select(Symbol).where(Symbol.project_id == project.id))
        }
        needle = normalize(term)
        result: list[CallEdge] = []
        for call in self.session.scalars(
            select(CallSite)
            .where(CallSite.project_id == project.id)
            .order_by(CallSite.source_file_id, CallSite.line)
        ):
            caller = (
                symbols.get(call.caller_symbol_id) if call.caller_symbol_id is not None else None
            )
            target = (
                symbols.get(call.resolved_symbol_id)
                if call.resolved_symbol_id is not None
                else None
            )
            caller_name = f"{modules[caller.module_id]}.{caller.name}" if caller else "<module>"
            callee_name = f"{modules[target.module_id]}.{target.name}" if target else call.full_name
            if needle not in normalize(callee_name if incoming else caller_name):
                continue
            result.append(
                CallEdge(
                    caller_name,
                    callee_name,
                    call.resolution,
                    self._file_path(files, call.source_file_id),
                    call.line,
                )
            )
        return result

    @staticmethod
    def _file_path(files: dict[int, str], file_id: int | None) -> str:
        if file_id is None:
            return "-"
        return files.get(file_id, "-")

    @staticmethod
    def _optional_file_path(files: dict[int, str], file_id: int | None) -> str | None:
        if file_id is None:
            return None
        return files.get(file_id)

    def _files(self, project_id: int) -> dict[int, str]:
        return {
            item.id: item.relative_path
            for item in self.session.scalars(
                select(SourceFile).where(SourceFile.project_id == project_id)
            )
        }

    def _count(self, model: type[Any], project_id: int, *conditions: Any) -> int:
        return int(
            self.session.scalar(
                select(func.count(model.id)).where(model.project_id == project_id, *conditions)
            )
            or 0
        )
