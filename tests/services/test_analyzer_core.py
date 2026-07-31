"""End-to-end analyzer test on a miniature Designer export."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from open1c_analyzer.services.analyzer import AnalyzerCore
from open1c_analyzer.services.knowledge import KnowledgeService
from open1c_analyzer.services.project_catalog import ProjectCatalog
from open1c_analyzer.storage.models import Base, CallSite


@contextmanager
def _session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def _metadata(path: Path, tag: str, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"<MetaDataObject><{tag}><Properties><Name>{name}</Name></Properties></{tag}></MetaDataObject>",
        encoding="utf-8",
    )


def test_builds_graph_and_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "configuration"
    _metadata(source / "CommonModules" / "Планирование.xml", "CommonModule", "Планирование")
    _metadata(source / "Documents" / "ЗаказПокупателя.xml", "Document", "ЗаказПокупателя")
    _metadata(
        source / "AccumulationRegisters" / "ОстаткиТоваров.xml",
        "AccumulationRegister",
        "ОстаткиТоваров",
    )
    common = source / "CommonModules" / "Планирование" / "Ext" / "Module.bsl"
    common.parent.mkdir(parents=True)
    common.write_text(
        """Функция ПолучитьОстаток(Склад) Экспорт
    Запрос = Новый Запрос;
    Запрос.Текст =
    "ВЫБРАТЬ
    | Остатки.КоличествоОстаток
    |ИЗ
    | РегистрНакопления.ОстаткиТоваров.Остатки(&Дата, Склад = &Склад) КАК Остатки";
    Сообщить("Проверка");
    Возврат Запрос.Выполнить();
КонецФункции""",
        encoding="utf-8",
    )
    obj = source / "Documents" / "ЗаказПокупателя" / "Ext" / "ObjectModule.bsl"
    obj.parent.mkdir(parents=True)
    obj.write_text(
        """Процедура ОбработкаПроведения(Отказ)
    Остаток = Планирование.ПолучитьОстаток(Склад);
КонецПроцедуры""",
        encoding="utf-8",
    )

    with _session() as session:
        ProjectCatalog(session).add_project("Demo", source)
        analyzer = AnalyzerCore(session)
        first = analyzer.analyze_project("Demo")

        def unexpected_resolve(_project: object) -> None:
            raise AssertionError("An unchanged project must reuse its existing graph")

        monkeypatch.setattr(analyzer, "_resolve", unexpected_resolve)
        second = analyzer.analyze_project("Demo")
        service = KnowledgeService(session)
        summary = service.summary("Demo")
        callers = service.callers("Demo", "ПолучитьОстаток")
        query_usage = service.query_usage("Demo", "ОстаткиТоваров")
        snapshot = service.export_snapshot("Demo", tmp_path / "snapshot.json")

    assert first.errors == ()
    assert first.metadata_objects == 3
    assert first.modules == 2
    assert first.symbols == 2
    assert first.queries == 1
    assert second.analyzed_files == 0
    assert second.skipped_files == 5
    assert second.graph_rebuilt is False
    assert summary.resolved_calls >= 1
    assert summary.built_in_calls >= 1
    assert callers[0].caller.endswith("ОбработкаПроведения")
    assert query_usage[0]["resolved"] is True
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["schema"] == "open1c-analyzer-snapshot-v1"
    assert payload["summary"]["symbols"] == 2


def test_metadata_conflict_isolated_to_one_file(tmp_path: Path) -> None:
    source = tmp_path / "configuration"
    _metadata(source / "A" / "Duplicate.xml", "FunctionalOption", "Duplicate")
    _metadata(source / "B" / "Duplicate.xml", "FunctionalOption", "Duplicate")
    _metadata(source / "C" / "After.xml", "CustomThing", "After")

    with _session() as session:
        ProjectCatalog(session).add_project("Conflicts", source)
        result = AnalyzerCore(session).analyze_project("Conflicts")

    assert result.analyzed_files == 2
    assert result.metadata_objects == 2
    assert len(result.errors) == 1
    assert "UNIQUE constraint failed" in result.errors[0]


def test_classifies_common_platform_globals(tmp_path: Path) -> None:
    source = tmp_path / "configuration"
    module = source / "CommonModules" / "Проверка" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    module.write_text(
        """Процедура ВыполнитьПроверку()
    Таблица = Новый Структура();
    Оповещение = Новый ОписаниеОповещения("ПослеПроверки", ЭтотОбъект);
    СтроковоеЗначение = СтрШаблон("%1", Лев("Тест", 1));
    ОткрытьФорму("ОбщаяФорма.Проверка");
    НачатьТранзакцию();
    ЗафиксироватьТранзакцию();
КонецПроцедуры
""",
        encoding="utf-8",
    )

    with _session() as session:
        ProjectCatalog(session).add_project("Platform", source)
        result = AnalyzerCore(session).analyze_project("Platform")
        summary = KnowledgeService(session).summary("Platform")

    assert result.errors == ()
    assert summary.unresolved_calls == 0
    assert summary.built_in_calls == 2
    assert summary.platform_calls == 5


def test_resolve_cleans_stale_parser_noise_and_classifies_more_platform_calls(
    tmp_path: Path,
) -> None:
    source = tmp_path / "configuration"
    module = source / "CommonModules" / "Проверка" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    module.write_text(
        """Процедура ВыполнитьПроверку()
    Значение = Год(ТекущаяДата());
    Оповестить("Проверка");
    Данные = Новый ФиксированнаяСтруктура("Год", Значение);
КонецПроцедуры
""",
        encoding="utf-8",
    )

    with _session() as session:
        project = ProjectCatalog(session).add_project("Platform2", source)
        AnalyzerCore(session).analyze_project("Platform2")
        source_file = ProjectCatalog(session).list_files(project)[0]
        session.add(
            CallSite(
                project_id=project.id,
                source_file_id=source_file.id,
                callee_name="И",
                normalized_name="и",
                qualifier=None,
                full_name="И",
                line=1,
                column=1,
                resolution="unresolved_unqualified",
            )
        )
        session.flush()

        result = AnalyzerCore(session).resolve_project("Platform2")
        calls = list(session.scalars(select(CallSite).where(CallSite.project_id == project.id)))

    assert result.calls == 4
    assert result.unresolved == 0
    assert result.built_in == 2
    assert result.platform == 2
    assert all(call.normalized_name != "и" for call in calls)
