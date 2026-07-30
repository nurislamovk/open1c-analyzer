"""End-to-end analyzer test on a miniature Designer export."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from open1c_analyzer.services.analyzer import AnalyzerCore
from open1c_analyzer.services.knowledge import KnowledgeService
from open1c_analyzer.services.project_catalog import ProjectCatalog
from open1c_analyzer.storage.models import Base


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


def test_builds_graph_and_snapshot(tmp_path: Path) -> None:
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
        first = AnalyzerCore(session).analyze_project("Demo")
        second = AnalyzerCore(session).analyze_project("Demo")
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
    assert summary.resolved_calls >= 1
    assert callers[0].caller.endswith("ОбработкаПроведения")
    assert query_usage[0]["resolved"] is True
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["schema"] == "open1c-analyzer-snapshot-v1"
    assert payload["summary"]["symbols"] == 2
