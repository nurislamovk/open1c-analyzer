"""Batching, atomic publication and SQLite reader regression tests."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text

from open1c_analyzer.services import analyzer as analyzer_module
from open1c_analyzer.services.analyzer import AnalysisProgress, AnalyzerCore
from open1c_analyzer.services.knowledge import KnowledgeService
from open1c_analyzer.services.project_catalog import ProjectCatalog
from open1c_analyzer.storage.database import Database
from open1c_analyzer.storage.models import Base, CallSite, MetadataObject, Reference


def _source(root: Path) -> Path:
    for folder, kind, name in (
        ("CommonModules", "CommonModule", "Расчеты"),
        ("Documents", "Document", "Заказ"),
        ("InformationRegisters", "InformationRegister", "Итоги"),
    ):
        directory = root / folder
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.xml").write_text(
            f"<MetaDataObject><{kind}><Properties><Name>{name}</Name>"
            f"</Properties></{kind}></MetaDataObject>",
            encoding="utf-8",
        )
    common = root / "CommonModules/Расчеты/Ext/Module.bsl"
    common.parent.mkdir(parents=True)
    common.write_text(
        "\n".join(
            f'Процедура Метод{index}() Экспорт\nСообщить("x");\nКонецПроцедуры'
            for index in range(7)
        ),
        encoding="utf-8",
    )
    obj = root / "Documents/Заказ/Ext/ObjectModule.bsl"
    obj.parent.mkdir(parents=True)
    obj.write_text(
        "Процедура ОбработкаПроведения()\n"
        + "\n".join(f"Расчеты.Метод{i}();" for i in range(7))
        + "\nНабор = РегистрыСведений.Итоги.СоздатьНаборЗаписей();\nНабор.Записать();\n"
        'Запрос = Новый Запрос;\nЗапрос.Текст = "ВЫБРАТЬ * ИЗ РегистрСведений.Итоги";\n'
        "КонецПроцедуры",
        encoding="utf-8",
    )
    return root


def _database(path: Path) -> Database:
    database = Database(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(database.engine)
    return database


@pytest.mark.parametrize("batch_size", [1, 2, 3])
def test_batch_boundaries_preserve_graph_and_cached_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch_size: int
) -> None:
    source = _source(tmp_path / "source")
    snapshots = []
    for name, size in (("default", 1000), ("small", batch_size)):
        monkeypatch.setattr(analyzer_module, "_BATCH_SIZE", size)
        database = _database(tmp_path / f"{name}.db")
        with database.session() as session:
            ProjectCatalog(session).add_project("Demo", source)
            progress: list[AnalysisProgress] = []
            analyzer = AnalyzerCore(session, progress=progress.append)
            result = analyzer.analyze_project("Demo")
            assert result.errors == ()
            cached_calls = list(session.scalars(select(CallSite)))
            cached_references = list(session.scalars(select(Reference)))
            analyzer.resolve_project("Demo")
            assert any(item.resolution == "qualified_module" for item in cached_calls)
            assert all(item.metadata_object_id for item in cached_references)
            assert {event.stage for event in progress} >= {
                "scan",
                "files",
                "calls",
                "references",
                "queries",
                "ready",
            }
            snapshot_path = KnowledgeService(session).export_snapshot(
                "Demo", tmp_path / f"{name}.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["project"].pop("last_analyzed_at")
            snapshots.append(snapshot)
        database.dispose()
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("scan", [False, True])
def test_wal_reader_sees_previous_index_and_failed_analysis_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scan: bool
) -> None:
    monkeypatch.setattr(analyzer_module, "_BATCH_SIZE", 2)
    source = _source(tmp_path / "source")
    database = _database(tmp_path / "atomic.db")
    with database.session() as session:
        ProjectCatalog(session).add_project("Demo", source)
        AnalyzerCore(session).analyze_project("Demo")
        previous = asdict(KnowledgeService(session).summary("Demo"))
    module = source / "CommonModules/Расчеты/Ext/Module.bsl"
    module.write_text(
        module.read_text(encoding="utf-8")
        + '\nПроцедура Новая() Экспорт\nСообщить("new");\nКонецПроцедуры',
        encoding="utf-8",
    )
    observed = []

    def during_write(event: AnalysisProgress) -> None:
        if event.stage == "calls" and event.completed:
            with database.session() as reader:
                assert reader.scalar(text("PRAGMA journal_mode")) == "wal"
                observed.append(asdict(KnowledgeService(reader).summary("Demo")))
            raise RuntimeError("Interrupted after a resolved batch")

    with pytest.raises(RuntimeError, match="Interrupted"), database.session() as session:
        AnalyzerCore(session, progress=during_write).analyze_project("Demo", force=True, scan=scan)
    with database.session() as reader:
        assert asdict(KnowledgeService(reader).summary("Demo")) == previous
    assert observed == [previous]
    database.dispose()


def test_metadata_longest_prefix_and_short_exact_aliases() -> None:
    parent = MetadataObject(id=1)
    child = MetadataObject(id=2)
    short = MetadataObject(id=3)
    aliases = {"abcde": parent, "abcdef": child, "ab": short}
    assert AnalyzerCore._resolve_metadata("ABCDEF.Поле", aliases) is child
    assert AnalyzerCore._resolve_metadata("ABCDE.Таблица", aliases) is parent
    assert AnalyzerCore._resolve_metadata("ab", aliases) is short
    assert AnalyzerCore._resolve_metadata("ab.Поле", aliases) is None
    assert AnalyzerCore._resolve_metadata("Отсутствует", aliases) is None


def test_reanalysis_invalidates_deleted_cached_facts_and_preserves_other_project(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "source")
    other_source = _source(tmp_path / "other")
    database = _database(tmp_path / "cached.db")
    with database.session() as session:
        catalog = ProjectCatalog(session)
        project = catalog.add_project("Demo", source)
        other = catalog.add_project("Other", other_source)
        analyzer = AnalyzerCore(session)
        original = analyzer.analyze_project("Demo")
        analyzer.analyze_project("Other")
        deleted = list(session.scalars(select(CallSite).where(CallSite.project_id == project.id)))
        retained = list(session.scalars(select(CallSite).where(CallSite.project_id == other.id)))
        retained_values = [(call.full_name, call.resolution) for call in retained]
        result = analyzer.analyze_project("Demo", force=True, scan=False)
        assert result.errors == ()
        assert (result.calls, result.dependencies) == (original.calls, original.dependencies)
        assert deleted and all(inspect(call).deleted for call in deleted)
        assert all(inspect(call).persistent for call in retained)
        assert [(call.full_name, call.resolution) for call in retained] == retained_values
    database.dispose()
