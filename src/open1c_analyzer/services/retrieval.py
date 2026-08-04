"""Focused retrieval, impact analysis and compact LLM context generation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from open1c_analyzer.parser.io import read_text
from open1c_analyzer.parser.names import normalize
from open1c_analyzer.services.project_catalog import ProjectCatalog, ProjectCatalogError
from open1c_analyzer.storage.models import (
    CallSite,
    Dependency,
    MetadataObject,
    Module,
    Project,
    QueryFact,
    SourceFile,
    Symbol,
)

EntityKind = Literal["metadata", "module", "symbol"]
Direction = Literal["incoming", "outgoing", "both"]


@dataclass(frozen=True, slots=True)
class EntityRef:
    project: str
    project_id: int
    kind: EntityKind
    id: int
    name: str
    file: str | None
    line_start: int | None = None
    line_end: int | None = None
    details: str = ""


@dataclass(frozen=True, slots=True)
class CallTrace:
    project: str
    caller: str
    callee: str
    resolution: str
    file: str
    line: int
    depth: int
    callee_project: str | None = None


@dataclass(frozen=True, slots=True)
class ImpactEdge:
    project: str
    source_kind: str
    source: str
    relation: str
    target_kind: str
    target: str
    resolved: bool
    file: str | None
    line: int | None
    depth: int


@dataclass(frozen=True, slots=True)
class AuditRow:
    category: str
    value: str
    count: int


@dataclass(frozen=True, slots=True)
class AuditReport:
    project: str
    calls: int
    resolved: int
    ambiguous: int
    built_in: int
    platform: int
    dynamic: int
    unresolved: int
    unresolved_dependencies: int
    groups: tuple[AuditRow, ...]


class RetrievalService:
    """Answer focused engineering questions without exporting the complete graph."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.catalog = ProjectCatalog(session)

    def find(
        self,
        project_name: str,
        term: str,
        *,
        include: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[EntityRef]:
        projects = self._projects(project_name, include)
        result: list[EntityRef] = []
        for project in projects:
            remaining = max(0, limit - len(result))
            if not remaining:
                break
            result.extend(self._find_project(project, term, remaining))
        return result

    def calls(
        self,
        project_name: str,
        term: str,
        *,
        direction: Direction = "both",
        depth: int = 1,
        include: tuple[str, ...] = (),
        limit: int = 200,
    ) -> list[CallTrace]:
        if depth < 1:
            raise ProjectCatalogError("Call depth must be at least 1.")
        projects = self._projects(project_name, include)
        project_by_id = {project.id: project for project in projects}
        project_ids = tuple(project_by_id)
        matches = self.find(project_name, term, include=include, limit=50)
        seed_frontier = self._seed_symbol_keys(
            matches,
            max_units=max(50, min(limit, 250)),
        )
        if not seed_frontier:
            return []

        traversal_directions: tuple[Literal["incoming", "outgoing"], ...] = (
            ("incoming", "outgoing") if direction == "both" else (direction,)
        )
        frontiers = {item: set(seed_frontier) for item in traversal_directions}
        visited = {item: set(seed_frontier) for item in traversal_directions}
        result: list[CallTrace] = []
        for level in range(1, depth + 1):
            if not any(frontiers.values()) or len(result) >= limit:
                break
            for traversal_direction in traversal_directions:
                frontier = frontiers[traversal_direction]
                if not frontier or len(result) >= limit:
                    continue
                rows = self._call_rows(
                    project_ids,
                    traversal_direction,
                    frontier,
                    limit - len(result),
                )
                result.extend(self._call_traces(rows, project_by_id, level))
                next_ids = {
                    symbol_id
                    for row in rows
                    for symbol_id in (
                        (row.caller_symbol_id,)
                        if traversal_direction == "incoming"
                        else (row.resolved_symbol_id,)
                    )
                    if symbol_id is not None
                }
                next_symbols = (
                    list(
                        self.session.scalars(select(Symbol).where(Symbol.id.in_(sorted(next_ids))))
                    )
                    if next_ids
                    else []
                )
                next_frontier = {(item.project_id, item.id) for item in next_symbols}

                # Keep the conservative fallback for databases resolved before projects were
                # linked explicitly. Once ``project resolve --include`` is run, these rows are
                # normal resolved calls and this branch produces nothing.
                if (
                    traversal_direction == "outgoing"
                    and len(project_ids) > 1
                    and len(result) < limit
                ):
                    cross_rows, cross_targets = self._cross_project_calls(
                        project_ids,
                        frontier,
                        project_by_id,
                        traversal_direction,
                        level,
                        limit - len(result),
                    )
                    result.extend(cross_rows)
                    next_frontier.update(cross_targets)
                frontiers[traversal_direction] = next_frontier - visited[traversal_direction]
                visited[traversal_direction].update(frontiers[traversal_direction])
        return self._deduplicate_calls(result)[:limit]

    def impact(
        self,
        project_name: str,
        term: str,
        *,
        depth: int = 2,
        include: tuple[str, ...] = (),
        limit: int = 500,
    ) -> list[ImpactEdge]:
        if depth < 1:
            raise ProjectCatalogError("Impact depth must be at least 1.")
        projects = self._projects(project_name, include)
        project_by_id = {project.id: project for project in projects}
        project_ids = tuple(project_by_id)
        seeds = self.find(project_name, term, include=include, limit=50)
        frontier = {(item.project_id, item.kind, item.id) for item in seeds}
        visited = set(frontier)
        result: list[ImpactEdge] = []
        files_by_project = {project_id: self._files(project_id) for project_id in project_ids}
        for level in range(1, depth + 1):
            if not frontier or len(result) >= limit:
                break
            next_frontier: set[tuple[int, EntityKind, int]] = set()
            by_kind: dict[EntityKind, set[int]] = {}
            for _project_id, kind, entity_id in frontier:
                by_kind.setdefault(kind, set()).add(entity_id)
            rows: list[Dependency] = []
            for kind, ids in by_kind.items():
                ordered_ids = sorted(ids)
                for offset in range(0, len(ordered_ids), 800):
                    if len(result) + len(rows) >= limit:
                        break
                    chunk = ordered_ids[offset : offset + 800]
                    batch = self.session.scalars(
                        select(Dependency)
                        .where(
                            Dependency.project_id.in_(project_ids),
                            Dependency.target_kind == kind,
                            Dependency.target_id.in_(chunk),
                        )
                        .order_by(
                            Dependency.project_id,
                            Dependency.source_name,
                            Dependency.relation,
                        )
                        .limit(limit - len(result) - len(rows))
                    )
                    rows.extend(batch)
            for row in rows:
                project = project_by_id[row.project_id]
                files = files_by_project[row.project_id]
                result.append(
                    ImpactEdge(
                        project.name,
                        row.source_kind,
                        row.source_name,
                        row.relation,
                        row.target_kind,
                        row.target_name,
                        row.is_resolved,
                        files.get(row.source_file_id) if row.source_file_id else None,
                        row.line,
                        level,
                    )
                )
                if row.source_id is not None and row.source_kind in {
                    "metadata",
                    "module",
                    "symbol",
                }:
                    source_kind = cast(EntityKind, row.source_kind)
                    next_frontier.add((row.project_id, source_kind, row.source_id))
            frontier = next_frontier - visited
            visited.update(frontier)
        return result[:limit]

    def audit(
        self,
        project_name: str,
        *,
        group_by: Literal["reason", "name", "qualifier"] = "reason",
        limit: int = 50,
    ) -> AuditReport:
        project = self.catalog.get_project(project_name)
        total = self._count(CallSite, project.id)
        resolved = self._count(CallSite, project.id, CallSite.resolved_symbol_id.is_not(None))
        ambiguous = self._count(CallSite, project.id, CallSite.resolution.like("ambiguous%"))
        built_in = self._count(CallSite, project.id, CallSite.resolution == "built_in")
        platform = self._count(CallSite, project.id, CallSite.resolution == "platform_api")
        dynamic = self._count(CallSite, project.id, CallSite.resolution == "dynamic_qualified")
        unresolved = self._count(CallSite, project.id, CallSite.resolution.like("unresolved%"))
        unresolved_dependencies = self._count(
            Dependency, project.id, Dependency.is_resolved.is_(False)
        )
        column: Any
        conditions: tuple[Any, ...]
        if group_by == "reason":
            column = CallSite.resolution
            category = "resolution"
            conditions = ()
        elif group_by == "name":
            column = CallSite.full_name
            category = "call"
            conditions = (CallSite.resolution.like("unresolved%"),)
        else:
            column = func.coalesce(CallSite.qualifier, "<without qualifier>")
            category = "qualifier"
            conditions = (CallSite.resolution.like("unresolved%"),)
        rows = self.session.execute(
            select(column, func.count(CallSite.id))
            .where(CallSite.project_id == project.id, *conditions)
            .group_by(column)
            .order_by(func.count(CallSite.id).desc(), column)
            .limit(limit)
        )
        groups = tuple(AuditRow(category, str(value), int(count)) for value, count in rows)
        return AuditReport(
            project.name,
            total,
            resolved,
            ambiguous,
            built_in,
            platform,
            dynamic,
            unresolved,
            unresolved_dependencies,
            groups,
        )

    def context(
        self,
        project_name: str,
        term: str,
        output: Path,
        *,
        include: tuple[str, ...] = (),
        depth: int = 1,
        max_chars: int = 60_000,
        max_units: int = 30,
    ) -> Path:
        projects = self._projects(project_name, include)
        project_by_id = {project.id: project for project in projects}
        matches = self.find(project_name, term, include=include, limit=20)
        call_rows = self.calls(
            project_name,
            term,
            direction="both",
            depth=depth,
            include=include,
            limit=300,
        )
        impact_rows = self.impact(
            project_name,
            term,
            depth=depth,
            include=include,
            limit=300,
        )
        seed_symbol_keys = self._seed_symbol_keys(matches, max_units=max_units)
        symbol_keys = set(seed_symbol_keys)
        project_by_name = {item.name: item for item in projects}
        for trace in call_rows:
            caller_project = project_by_name[trace.project]
            symbol_keys.update(self._symbol_keys_by_names(caller_project.id, (trace.caller,)))
            callee_project = project_by_name.get(trace.callee_project or trace.project)
            if callee_project is not None:
                symbol_keys.update(self._symbol_keys_by_names(callee_project.id, (trace.callee,)))
        for edge in impact_rows:
            if edge.source_kind != "symbol":
                continue
            project = next(item for item in projects if item.name == edge.project)
            symbol_keys.update(self._symbol_keys_by_names(project.id, (edge.source,)))
        source_units = self._source_units(
            project_by_id,
            symbol_keys,
            max_chars,
            max_units,
            primary_project_id=projects[0].id,
            priority_keys=seed_symbol_keys,
        )
        selected_symbol_ids: dict[int, set[int]] = {}
        for unit in source_units:
            selected_symbol_ids.setdefault(int(unit["project_id"]), set()).add(
                int(unit["symbol_id"])
            )
        context_outgoing = self._context_outgoing_impact(
            matches,
            selected_symbol_ids,
            project_by_id,
            limit=max(0, 300 - len(impact_rows)),
        )
        impact_rows = self._deduplicate_impact([*impact_rows, *context_outgoing])[:300]
        queries = self._queries(selected_symbol_ids, project_by_id)
        unresolved = self._unresolved_near(selected_symbol_ids, project_by_id)
        payload = {
            "schema": "open1c-analyzer-context-v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "request": {
                "project": project_name,
                "include": list(include),
                "term": term,
                "depth": depth,
                "max_chars": max_chars,
            },
            "projects": [
                {
                    "name": project.name,
                    "source_path": project.source_path,
                    "profile": json.loads(project.profile_json or "{}"),
                    "last_analyzed_at": (
                        project.last_analyzed_at.isoformat() if project.last_analyzed_at else None
                    ),
                }
                for project in projects
            ],
            "matches": [asdict(item) for item in matches],
            "source_units": source_units,
            "calls": [asdict(item) for item in call_rows],
            "impact": [asdict(item) for item in impact_rows],
            "queries": queries,
            "unresolved_near_context": unresolved,
            "limits": {
                "source_units": max_units,
                "source_characters": max_chars,
                "actual_source_characters": sum(len(str(item["source"])) for item in source_units),
            },
        }
        target = output.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return target

    def _context_outgoing_impact(
        self,
        matches: list[EntityRef],
        selected_symbol_ids: dict[int, set[int]],
        projects: dict[int, Project],
        *,
        limit: int,
    ) -> list[ImpactEdge]:
        if limit <= 0:
            return []
        seeds: dict[int, dict[EntityKind, set[int]]] = {}
        for item in matches:
            seeds.setdefault(item.project_id, {}).setdefault(item.kind, set()).add(item.id)
        for project_id, symbol_ids in selected_symbol_ids.items():
            seeds.setdefault(project_id, {}).setdefault("symbol", set()).update(symbol_ids)

        result: list[ImpactEdge] = []
        for project_id, by_kind in seeds.items():
            remaining = limit - len(result)
            if remaining <= 0:
                break
            conditions = [
                and_(Dependency.source_kind == kind, Dependency.source_id.in_(sorted(ids)))
                for kind, ids in by_kind.items()
                if ids
            ]
            if not conditions:
                continue
            rows = list(
                self.session.scalars(
                    select(Dependency)
                    .where(Dependency.project_id == project_id, or_(*conditions))
                    .order_by(Dependency.source_name, Dependency.relation, Dependency.target_name)
                    .limit(remaining)
                )
            )
            files = self._files(project_id)
            project = projects[project_id]
            for row in rows:
                result.append(
                    ImpactEdge(
                        project.name,
                        row.source_kind,
                        row.source_name,
                        row.relation,
                        row.target_kind,
                        row.target_name,
                        row.is_resolved,
                        files.get(row.source_file_id) if row.source_file_id else None,
                        row.line,
                        1,
                    )
                )
        return result

    @staticmethod
    def _deduplicate_impact(rows: list[ImpactEdge]) -> list[ImpactEdge]:
        result: list[ImpactEdge] = []
        seen: set[tuple[object, ...]] = set()
        for row in rows:
            key = (
                row.project,
                row.source_kind,
                row.source,
                row.relation,
                row.target_kind,
                row.target,
                row.file,
                row.line,
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result

    @staticmethod
    def _deduplicate_calls(rows: list[CallTrace]) -> list[CallTrace]:
        result: list[CallTrace] = []
        seen: set[tuple[object, ...]] = set()
        for row in rows:
            key = (
                row.project,
                row.caller,
                row.callee,
                row.resolution,
                row.file,
                row.line,
                row.depth,
                row.callee_project,
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result

    def _seed_symbol_keys(
        self,
        matches: list[EntityRef],
        *,
        max_units: int,
    ) -> set[tuple[int, int]]:
        result = {(item.project_id, item.id) for item in matches if item.kind == "symbol"}
        module_ids_by_project: dict[int, set[int]] = {}
        metadata_ids_by_project: dict[int, set[int]] = {}
        for item in matches:
            if item.kind == "module":
                module_ids_by_project.setdefault(item.project_id, set()).add(item.id)
            elif item.kind == "metadata":
                metadata_ids_by_project.setdefault(item.project_id, set()).add(item.id)

        project_ids = set(module_ids_by_project) | set(metadata_ids_by_project)
        remaining = max(0, max_units * 4 - len(result))
        for project_id in sorted(project_ids):
            if remaining <= 0:
                break
            module_ids = set(module_ids_by_project.get(project_id, set()))
            metadata_ids = metadata_ids_by_project.get(project_id, set())
            if metadata_ids:
                module_ids.update(
                    self.session.scalars(
                        select(Module.id).where(
                            Module.project_id == project_id,
                            Module.metadata_object_id.in_(sorted(metadata_ids)),
                        )
                    )
                )
            if not module_ids:
                continue
            rows = self.session.execute(
                select(Symbol.project_id, Symbol.id)
                .where(
                    Symbol.project_id == project_id,
                    Symbol.module_id.in_(sorted(module_ids)),
                )
                .order_by(
                    Symbol.is_export.desc(),
                    Symbol.source_file_id,
                    Symbol.line_start,
                )
                .limit(remaining)
            )
            for symbol_project_id, symbol_id in rows:
                result.add((int(symbol_project_id), int(symbol_id)))
            remaining = max(0, max_units * 4 - len(result))
        return result

    def _find_project(self, project: Project, term: str, limit: int) -> list[EntityRef]:
        files = self._files(project.id)
        needle = normalize(term)
        exact_last = normalize(term.rsplit(".", 1)[-1])
        result: list[EntityRef] = []

        symbols = list(
            self.session.scalars(
                select(Symbol)
                .where(Symbol.project_id == project.id, Symbol.normalized_name == exact_last)
                .order_by(Symbol.is_export.desc(), Symbol.name)
                .limit(limit * 4)
            )
        )
        modules = self._modules_for_symbols(symbols)
        for symbol in symbols:
            module = modules[symbol.module_id]
            full_name = f"{module.full_name}.{symbol.name}"
            if needle not in normalize(full_name) and needle != symbol.normalized_name:
                continue
            result.append(self._symbol_ref(project, symbol, module, files))
            if len(result) >= limit:
                return result

        exact_metadata = list(
            self.session.scalars(
                select(MetadataObject)
                .where(
                    MetadataObject.project_id == project.id,
                    or_(MetadataObject.name == term, MetadataObject.full_name == term),
                )
                .order_by(MetadataObject.full_name)
                .limit(limit - len(result))
            )
        )
        for metadata_item in exact_metadata:
            result.append(self._metadata_ref(project, metadata_item, files))
        if len(result) >= limit:
            return result

        exact_modules = list(
            self.session.scalars(
                select(Module)
                .where(
                    Module.project_id == project.id,
                    or_(Module.name == term, Module.full_name == term),
                )
                .order_by(Module.full_name)
                .limit(limit - len(result))
            )
        )
        for module_item in exact_modules:
            result.append(self._module_ref(project, module_item, files))
        if result:
            return self._deduplicate(result)[:limit]

        # Fallback contains search is intentionally bounded. Exact symbol lookup above is indexed
        # and is the normal path for engineering commands on large configurations.
        pattern = f"%{term}%"
        metadata = self.session.scalars(
            select(MetadataObject)
            .where(
                MetadataObject.project_id == project.id,
                or_(
                    MetadataObject.name.like(pattern),
                    MetadataObject.full_name.like(pattern),
                    MetadataObject.synonym.like(pattern),
                ),
            )
            .order_by(MetadataObject.full_name)
            .limit(limit)
        )
        result.extend(self._metadata_ref(project, item, files) for item in metadata)
        if len(result) < limit:
            module_rows = self.session.scalars(
                select(Module)
                .where(
                    Module.project_id == project.id,
                    or_(Module.name.like(pattern), Module.full_name.like(pattern)),
                )
                .order_by(Module.full_name)
                .limit(limit - len(result))
            )
            result.extend(self._module_ref(project, item, files) for item in module_rows)
        if len(result) < limit:
            symbol_rows = list(
                self.session.scalars(
                    select(Symbol)
                    .where(Symbol.project_id == project.id, Symbol.name.like(pattern))
                    .order_by(Symbol.name)
                    .limit(limit - len(result))
                )
            )
            symbol_modules = self._modules_for_symbols(symbol_rows)
            result.extend(
                self._symbol_ref(project, item, symbol_modules[item.module_id], files)
                for item in symbol_rows
            )
        return self._deduplicate(result)[:limit]

    def _call_rows(
        self,
        project_ids: tuple[int, ...],
        direction: Literal["incoming", "outgoing"],
        frontier: set[tuple[int, int]],
        limit: int,
    ) -> list[CallSite]:
        result: list[CallSite] = []
        if direction == "outgoing":
            by_project: dict[int, set[int]] = {}
            for project_id, symbol_id in frontier:
                by_project.setdefault(project_id, set()).add(symbol_id)
            for project_id, symbol_ids in by_project.items():
                ordered_ids = sorted(symbol_ids)
                for offset in range(0, len(ordered_ids), 800):
                    if len(result) >= limit:
                        break
                    chunk = ordered_ids[offset : offset + 800]
                    rows = self.session.scalars(
                        select(CallSite)
                        .where(
                            CallSite.project_id == project_id,
                            CallSite.caller_symbol_id.in_(chunk),
                        )
                        .order_by(CallSite.source_file_id, CallSite.line)
                        .limit(limit - len(result))
                    )
                    result.extend(rows)
            return result

        target_ids = sorted({symbol_id for _project_id, symbol_id in frontier})
        for source_project_id in project_ids:
            if len(result) >= limit:
                break
            for offset in range(0, len(target_ids), 800):
                if len(result) >= limit:
                    break
                chunk = target_ids[offset : offset + 800]
                rows = self.session.scalars(
                    select(CallSite)
                    .where(
                        CallSite.project_id == source_project_id,
                        CallSite.resolved_symbol_id.in_(chunk),
                    )
                    .order_by(CallSite.source_file_id, CallSite.line)
                    .limit(limit - len(result))
                )
                result.extend(rows)
        return result

    def _call_traces(
        self,
        rows: list[CallSite],
        projects: dict[int, Project],
        depth: int,
    ) -> list[CallTrace]:
        if not rows:
            return []
        project_ids = {row.project_id for row in rows}
        symbol_ids = {
            symbol_id
            for row in rows
            for symbol_id in (row.caller_symbol_id, row.resolved_symbol_id)
            if symbol_id is not None
        }
        symbols = {
            item.id: item
            for item in self.session.scalars(
                select(Symbol).where(Symbol.id.in_(sorted(symbol_ids)))
            )
        }
        modules = self._modules_for_symbols(list(symbols.values()))
        files: dict[int, dict[int, str]] = {
            project_id: self._files(project_id) for project_id in project_ids
        }
        result: list[CallTrace] = []
        for row in rows:
            caller = symbols.get(row.caller_symbol_id) if row.caller_symbol_id is not None else None
            target = (
                symbols.get(row.resolved_symbol_id) if row.resolved_symbol_id is not None else None
            )
            caller_name = self._symbol_full_name(caller, modules) if caller else "<module>"
            callee_name = self._symbol_full_name(target, modules) if target else row.full_name
            target_project = projects.get(target.project_id) if target is not None else None
            result.append(
                CallTrace(
                    projects[row.project_id].name,
                    caller_name,
                    callee_name,
                    row.resolution,
                    files[row.project_id].get(row.source_file_id, "-"),
                    row.line,
                    depth,
                    target_project.name if target_project is not None else None,
                )
            )
        return result

    def _cross_project_calls(
        self,
        project_ids: tuple[int, ...],
        frontier: set[tuple[int, int]],
        projects: dict[int, Project],
        direction: Direction,
        depth: int,
        limit: int,
    ) -> tuple[list[CallTrace], set[tuple[int, int]]]:
        if direction == "incoming":
            return [], set()
        frontier_ids = {symbol_id for _project_id, symbol_id in frontier}
        calls = list(
            self.session.scalars(
                select(CallSite)
                .where(
                    CallSite.project_id.in_(project_ids),
                    CallSite.caller_symbol_id.in_(sorted(frontier_ids)),
                    CallSite.resolved_symbol_id.is_(None),
                )
                .limit(limit)
            )
        )
        if not calls:
            return [], set()
        names = {row.normalized_name for row in calls}
        candidates = list(
            self.session.scalars(
                select(Symbol).where(
                    Symbol.project_id.in_(project_ids),
                    Symbol.normalized_name.in_(sorted(names)),
                    Symbol.is_export.is_(True),
                )
            )
        )
        by_name: dict[str, list[Symbol]] = {}
        for candidate in candidates:
            by_name.setdefault(candidate.normalized_name, []).append(candidate)
        caller_ids: set[int] = set()
        for call in calls:
            caller_id = call.caller_symbol_id
            if caller_id is not None:
                caller_ids.add(caller_id)
        symbols = {
            item.id: item
            for item in self.session.scalars(
                select(Symbol).where(
                    Symbol.id.in_(sorted(caller_ids | {item.id for item in candidates}))
                )
            )
        }
        modules = self._modules_for_symbols(list(symbols.values()))
        files = {project_id: self._files(project_id) for project_id in project_ids}
        traces: list[CallTrace] = []
        targets: set[tuple[int, int]] = set()
        for call in calls:
            matching = by_name.get(call.normalized_name, [])
            if call.qualifier:
                qualifier = normalize(call.qualifier.split(".")[0])
                matching = [
                    item
                    for item in matching
                    if normalize(modules[item.module_id].name) == qualifier
                ]
            if len(matching) != 1:
                continue
            target = matching[0]
            caller = symbols.get(call.caller_symbol_id) if call.caller_symbol_id else None
            traces.append(
                CallTrace(
                    projects[call.project_id].name,
                    self._symbol_full_name(caller, modules) if caller else "<module>",
                    self._symbol_full_name(target, modules),
                    "cross_project_candidate",
                    files[call.project_id].get(call.source_file_id, "-"),
                    call.line,
                    depth,
                    projects[target.project_id].name,
                )
            )
            targets.add((target.project_id, target.id))
        return traces, targets

    def _source_units(
        self,
        projects: dict[int, Project],
        symbol_keys: set[tuple[int, int]],
        max_chars: int,
        max_units: int,
        *,
        primary_project_id: int,
        priority_keys: set[tuple[int, int]] | None = None,
    ) -> list[dict[str, Any]]:
        if not symbol_keys:
            return []
        symbol_ids = {symbol_id for _project_id, symbol_id in symbol_keys}
        symbols = list(
            self.session.scalars(select(Symbol).where(Symbol.id.in_(sorted(symbol_ids))))
        )
        modules = self._modules_for_symbols(symbols)
        files_by_project = {project_id: self._source_files(project_id) for project_id in projects}
        result: list[dict[str, Any]] = []
        used = 0
        priority = priority_keys or set()
        for symbol in sorted(
            symbols,
            key=lambda item: (
                0 if (item.project_id, item.id) in priority else 1,
                0 if item.project_id == primary_project_id else 1,
                item.project_id,
                item.source_file_id,
                item.line_start,
            ),
        ):
            if len(result) >= max_units or used >= max_chars:
                break
            project = projects[symbol.project_id]
            source_file = files_by_project[symbol.project_id].get(symbol.source_file_id)
            if source_file is None or not project.source_path:
                continue
            source_path = Path(project.source_path).joinpath(
                *PurePosixPath(source_file.relative_path).parts
            )
            try:
                text = read_text(source_path)
            except OSError:
                continue
            lines = text.splitlines()
            start = max(1, symbol.line_start - 3)
            end = min(len(lines), symbol.line_end + 3)
            source = "\n".join(lines[start - 1 : end])
            remaining = max_chars - used
            if len(source) > remaining:
                source = source[:remaining]
            module = modules[symbol.module_id]
            result.append(
                {
                    "project": project.name,
                    "project_id": project.id,
                    "symbol_id": symbol.id,
                    "symbol": f"{module.full_name}.{symbol.name}",
                    "kind": symbol.kind,
                    "export": symbol.is_export,
                    "directive": symbol.directive,
                    "region": symbol.region,
                    "signature": symbol.signature,
                    "parameters": json.loads(symbol.parameters_json),
                    "file": source_file.relative_path,
                    "line_start": start,
                    "line_end": end,
                    "source": source,
                }
            )
            used += len(source)
        return result

    def _queries(
        self,
        symbol_ids: dict[int, set[int]],
        projects: dict[int, Project],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for project_id, ids in symbol_ids.items():
            files = self._files(project_id)
            rows = self.session.scalars(
                select(QueryFact)
                .where(QueryFact.project_id == project_id, QueryFact.symbol_id.in_(sorted(ids)))
                .order_by(QueryFact.source_file_id, QueryFact.line_start)
                .limit(100)
            )
            for row in rows:
                result.append(
                    {
                        "project": projects[project_id].name,
                        "file": files.get(row.source_file_id),
                        "line_start": row.line_start,
                        "line_end": row.line_end,
                        "kind": row.kind,
                        "tables": json.loads(row.tables_json),
                        "text": row.text,
                    }
                )
        return result

    def _unresolved_near(
        self,
        symbol_ids: dict[int, set[int]],
        projects: dict[int, Project],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for project_id, ids in symbol_ids.items():
            files = self._files(project_id)
            rows = self.session.scalars(
                select(CallSite)
                .where(
                    CallSite.project_id == project_id,
                    CallSite.caller_symbol_id.in_(sorted(ids)),
                    CallSite.resolved_symbol_id.is_(None),
                    CallSite.resolution.not_in(("built_in", "platform_api")),
                )
                .order_by(CallSite.source_file_id, CallSite.line)
                .limit(200)
            )
            for row in rows:
                result.append(
                    {
                        "project": projects[project_id].name,
                        "call": row.full_name,
                        "resolution": row.resolution,
                        "file": files.get(row.source_file_id),
                        "line": row.line,
                    }
                )
        return result

    def _symbol_keys_by_names(
        self,
        project_id: int,
        names: tuple[str, ...],
    ) -> set[tuple[int, int]]:
        result: set[tuple[int, int]] = set()
        for name in names:
            if name == "<module>":
                continue
            if "." in name:
                module_name, symbol_name = name.rsplit(".", 1)
                rows = self.session.execute(
                    select(Symbol.project_id, Symbol.id)
                    .join(Module, Module.id == Symbol.module_id)
                    .where(
                        Symbol.project_id == project_id,
                        Symbol.normalized_name == normalize(symbol_name),
                        Module.full_name == module_name,
                    )
                )
                result.update(
                    (int(row_project_id), int(symbol_id)) for row_project_id, symbol_id in rows
                )
                continue

            symbols = list(
                self.session.scalars(
                    select(Symbol).where(
                        Symbol.project_id == project_id,
                        Symbol.normalized_name == normalize(name),
                    )
                )
            )
            if len(symbols) == 1:
                result.add((project_id, symbols[0].id))
        return result

    def _projects(self, primary: str, include: tuple[str, ...]) -> list[Project]:
        names = (primary, *include)
        result: list[Project] = []
        seen: set[int] = set()
        for name in names:
            project = self.catalog.get_project(name)
            if project.id not in seen:
                result.append(project)
                seen.add(project.id)
        return result

    def _files(self, project_id: int) -> dict[int, str]:
        return {
            item.id: item.relative_path
            for item in self.session.scalars(
                select(SourceFile).where(SourceFile.project_id == project_id)
            )
        }

    def _source_files(self, project_id: int) -> dict[int, SourceFile]:
        return {
            item.id: item
            for item in self.session.scalars(
                select(SourceFile).where(SourceFile.project_id == project_id)
            )
        }

    def _modules_for_symbols(self, symbols: list[Symbol]) -> dict[int, Module]:
        module_ids = {item.module_id for item in symbols}
        if not module_ids:
            return {}
        return {
            item.id: item
            for item in self.session.scalars(
                select(Module).where(Module.id.in_(sorted(module_ids)))
            )
        }

    @staticmethod
    def _symbol_full_name(symbol: Symbol | None, modules: dict[int, Module]) -> str:
        if symbol is None:
            return "<module>"
        return f"{modules[symbol.module_id].full_name}.{symbol.name}"

    @staticmethod
    def _symbol_ref(
        project: Project,
        symbol: Symbol,
        module: Module,
        files: dict[int, str],
    ) -> EntityRef:
        return EntityRef(
            project.name,
            project.id,
            "symbol",
            symbol.id,
            f"{module.full_name}.{symbol.name}",
            files.get(symbol.source_file_id),
            symbol.line_start,
            symbol.line_end,
            (
                f"{symbol.kind}; export={symbol.is_export}; directive={symbol.directive or '-'}; "
                f"region={symbol.region or '-'}"
            ),
        )

    @staticmethod
    def _metadata_ref(
        project: Project,
        item: MetadataObject,
        files: dict[int, str],
    ) -> EntityRef:
        return EntityRef(
            project.name,
            project.id,
            "metadata",
            item.id,
            item.full_name,
            files.get(item.source_file_id),
            details=f"kind={item.kind}; synonym={item.synonym or '-'}",
        )

    @staticmethod
    def _module_ref(
        project: Project,
        item: Module,
        files: dict[int, str],
    ) -> EntityRef:
        return EntityRef(
            project.name,
            project.id,
            "module",
            item.id,
            item.full_name,
            files.get(item.source_file_id),
            details=f"kind={item.module_kind}; owner={item.owner_name or '-'}",
        )

    @staticmethod
    def _deduplicate(items: list[EntityRef]) -> list[EntityRef]:
        result: list[EntityRef] = []
        seen: set[tuple[int, str, int]] = set()
        for item in items:
            key = (item.project_id, item.kind, item.id)
            if key not in seen:
                result.append(item)
                seen.add(key)
        return result

    def _count(self, model: type[Any], project_id: int, *conditions: Any) -> int:
        return int(
            self.session.scalar(
                select(func.count(model.id)).where(model.project_id == project_id, *conditions)
            )
            or 0
        )
