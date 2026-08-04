"""Resolution, focused retrieval, impact analysis and LLM context tests."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from open1c_analyzer.services.analyzer import AnalyzerCore
from open1c_analyzer.services.project_catalog import ProjectCatalog
from open1c_analyzer.services.retrieval import RetrievalService
from open1c_analyzer.storage.models import Base, CallSite, Symbol


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


def _configuration(tmp_path: Path) -> Path:
    source = tmp_path / "configuration"
    _metadata(source / "CommonModules" / "Расчеты.xml", "CommonModule", "Расчеты")
    _metadata(source / "Documents" / "Заказ.xml", "Document", "Заказ")
    _metadata(
        source / "AccumulationRegisters" / "Запасы.xml",
        "AccumulationRegister",
        "Запасы",
    )
    common = source / "CommonModules" / "Расчеты" / "Ext" / "Module.bsl"
    common.parent.mkdir(parents=True)
    common.write_text(
        """Функция ПолучитьОстаток(Склад) Экспорт
    Сообщить("Расчет");
    Возврат 1;
КонецФункции

Процедура ВнутренняяПроверка()
    ПолучитьОстаток(Неопределено);
КонецПроцедуры""",
        encoding="utf-8",
    )
    document = source / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl"
    document.parent.mkdir(parents=True)
    document.write_text(
        """Процедура ОбработкаПроведения(Отказ)
    Остаток = Расчеты.ПолучитьОстаток(Склад);
    Данные.НеизвестныйМетод();
    НовыйДокумент = Документы.Заказ.СоздатьДокумент();
    Запрос = Новый Запрос;
    Запрос.Текст =
    "ВЫБРАТЬ * ИЗ РегистрНакопления.Запасы.Остатки() КАК Запасы";
КонецПроцедуры""",
        encoding="utf-8",
    )
    return source


def test_resolution_retrieval_impact_and_context(tmp_path: Path) -> None:
    source = _configuration(tmp_path)
    context_path = tmp_path / "context.json"

    with _session() as session:
        ProjectCatalog(session).add_project("Demo", source)
        analyzer = AnalyzerCore(session)
        analysis = analyzer.analyze_project("Demo")
        resolution = analyzer.resolve_project("Demo")
        service = RetrievalService(session)
        matches = service.find("Demo", "ПолучитьОстаток")
        incoming = service.calls(
            "Demo",
            "ПолучитьОстаток",
            direction="incoming",
            depth=1,
        )
        outgoing = service.calls(
            "Demo",
            "ОбработкаПроведения",
            direction="outgoing",
            depth=1,
        )
        impact = service.impact("Demo", "РегистрНакопления.Запасы", depth=2)
        audit = service.audit("Demo", group_by="reason")
        written = service.context(
            "Demo",
            "ОбработкаПроведения",
            context_path,
            depth=1,
            max_chars=20_000,
        )
        statuses = {
            item.full_name: item.resolution
            for item in session.scalars(select(CallSite).order_by(CallSite.line))
        }

    assert analysis.errors == ()
    assert resolution.resolved >= 2
    assert resolution.built_in >= 1
    assert resolution.platform >= 1
    assert resolution.dynamic >= 1
    assert matches[0].name.endswith("ПолучитьОстаток")
    assert any(row.caller.endswith("ОбработкаПроведения") for row in incoming)
    assert any(row.callee.endswith("ПолучитьОстаток") for row in outgoing)
    assert any(row.target == "РегистрНакопления.Запасы" for row in impact)
    assert any(row.value == "dynamic_qualified" for row in audit.groups)
    assert statuses["Данные.НеизвестныйМетод"] == "dynamic_qualified"
    assert statuses["Документы.Заказ.СоздатьДокумент"] == "platform_api"
    assert written == context_path.resolve()
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "open1c-analyzer-context-v1"
    assert payload["source_units"]
    assert any("ОбработкаПроведения" in item["source"] for item in payload["source_units"])
    assert payload["queries"][0]["tables"][0]["name"] == "РегистрНакопления.Запасы.Остатки"


def test_calls_for_metadata_expand_to_owned_module_symbols(tmp_path: Path) -> None:
    source = _configuration(tmp_path)

    with _session() as session:
        ProjectCatalog(session).add_project("Demo", source)
        AnalyzerCore(session).analyze_project("Demo")
        rows = RetrievalService(session).calls(
            "Demo",
            "ОбщийМодуль.Расчеты",
            direction="both",
            depth=1,
        )

    assert rows
    assert any(row.callee.endswith("ПолучитьОстаток") for row in rows)


def test_cross_project_candidate_for_extension_scope(tmp_path: Path) -> None:
    base = tmp_path / "base"
    extension = tmp_path / "extension"
    _metadata(base / "CommonModules" / "ОбщийAPI.xml", "CommonModule", "ОбщийAPI")
    base_module = base / "CommonModules" / "ОбщийAPI" / "Ext" / "Module.bsl"
    base_module.parent.mkdir(parents=True)
    base_module.write_text(
        "Процедура ВыполнитьРаботу() Экспорт\nКонецПроцедуры",
        encoding="utf-8",
    )
    _metadata(extension / "DataProcessors" / "Расширение.xml", "DataProcessor", "Расширение")
    extension_module = extension / "DataProcessors" / "Расширение" / "Ext" / "ObjectModule.bsl"
    extension_module.parent.mkdir(parents=True)
    extension_module.write_text(
        """Процедура Запустить()
    ОбщийAPI.ВыполнитьРаботу();
КонецПроцедуры""",
        encoding="utf-8",
    )

    with _session() as session:
        catalog = ProjectCatalog(session)
        catalog.add_project("Base", base)
        catalog.add_project("Extension", extension)
        AnalyzerCore(session).analyze_project("Base")
        AnalyzerCore(session).analyze_project("Extension")
        rows = RetrievalService(session).calls(
            "Extension",
            "Запустить",
            direction="outgoing",
            include=("Base",),
        )

    assert any(row.resolution == "cross_project_candidate" for row in rows)
    assert any(row.callee.endswith("ОбщийAPI.ВыполнитьРаботу") for row in rows)


def test_context_for_metadata_seeds_its_module_symbols(tmp_path: Path) -> None:
    source = _configuration(tmp_path)
    context_path = tmp_path / "module-context.json"

    with _session() as session:
        ProjectCatalog(session).add_project("Demo", source)
        AnalyzerCore(session).analyze_project("Demo")
        written = RetrievalService(session).context(
            "Demo",
            "ОбщийМодуль.Расчеты",
            context_path,
            depth=1,
            max_chars=20_000,
            max_units=10,
        )

    payload = json.loads(written.read_text(encoding="utf-8"))
    sources = "\n".join(item["source"] for item in payload["source_units"])
    assert "ПолучитьОстаток" in sources
    assert "ВнутренняяПроверка" in sources
    assert payload["calls"]
    assert any(item["callee"].endswith("ПолучитьОстаток") for item in payload["calls"])
    assert payload["impact"]
    assert any(item["relation"] == "contains_module" for item in payload["impact"])


def test_calls_both_keeps_incoming_and_outgoing_traversal_separate(tmp_path: Path) -> None:
    source = tmp_path / "configuration"
    for name in ("АХаб", "Цель", "ЧужойМодуль"):
        _metadata(source / "CommonModules" / f"{name}.xml", "CommonModule", name)

    hub = source / "CommonModules" / "АХаб" / "Ext" / "Module.bsl"
    hub.parent.mkdir(parents=True)
    hub.write_text(
        """Процедура Общая() Экспорт
    Внутренняя();
КонецПроцедуры

Процедура Внутренняя()
КонецПроцедуры""",
        encoding="utf-8",
    )
    target = source / "CommonModules" / "Цель" / "Ext" / "Module.bsl"
    target.parent.mkdir(parents=True)
    target.write_text(
        """Процедура Старт() Экспорт
    АХаб.Общая();
КонецПроцедуры""",
        encoding="utf-8",
    )
    unrelated = source / "CommonModules" / "ЧужойМодуль" / "Ext" / "Module.bsl"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(
        """Процедура ЧужойВызов() Экспорт
    АХаб.Общая();
КонецПроцедуры""",
        encoding="utf-8",
    )

    with _session() as session:
        ProjectCatalog(session).add_project("Demo", source)
        AnalyzerCore(session).analyze_project("Demo")
        rows = RetrievalService(session).calls(
            "Demo",
            "ОбщийМодуль.Цель",
            direction="both",
            depth=2,
            limit=100,
        )

    assert any(row.callee.endswith("АХаб.Общая") for row in rows)
    assert any(row.callee.endswith("АХаб.Внутренняя") for row in rows)
    assert not any("ЧужойВызов" in row.caller for row in rows)


def test_context_prioritizes_requested_module_before_graph_neighbors(tmp_path: Path) -> None:
    source = tmp_path / "configuration"
    for name in ("АХаб", "Цель"):
        _metadata(source / "CommonModules" / f"{name}.xml", "CommonModule", name)

    hub = source / "CommonModules" / "АХаб" / "Ext" / "Module.bsl"
    hub.parent.mkdir(parents=True)
    hub.write_text(
        "Процедура Общая() Экспорт\n"
        + '    Текст = "'
        + ("ОченьДлинныйТекст" * 100)
        + '";\nКонецПроцедуры',
        encoding="utf-8",
    )
    target = source / "CommonModules" / "Цель" / "Ext" / "Module.bsl"
    target.parent.mkdir(parents=True)
    target.write_text(
        """Процедура Старт() Экспорт
    АХаб.Общая();
КонецПроцедуры""",
        encoding="utf-8",
    )
    context_path = tmp_path / "priority-context.json"

    with _session() as session:
        ProjectCatalog(session).add_project("Demo", source)
        AnalyzerCore(session).analyze_project("Demo")
        written = RetrievalService(session).context(
            "Demo",
            "ОбщийМодуль.Цель",
            context_path,
            depth=2,
            max_chars=300,
            max_units=10,
        )

    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["source_units"]
    assert payload["source_units"][0]["symbol"].startswith("Цель.")
    assert "Процедура Старт" in payload["source_units"][0]["source"]
    assert not any("Чужой" in item["caller"] for item in payload["calls"])


def test_explicit_cross_project_resolution_and_traversal(tmp_path: Path) -> None:
    base = tmp_path / "base"
    extension = tmp_path / "extension"
    _metadata(
        base / "CommonModules" / "ПечатьДокументовУНФ.xml", "CommonModule", "ПечатьДокументовУНФ"
    )
    base_module = base / "CommonModules" / "ПечатьДокументовУНФ" / "Ext" / "Module.bsl"
    base_module.parent.mkdir(parents=True)
    base_module.write_text(
        "Функция ПолучитьОбластьБезопасно(Макет, ИмяОбласти) Экспорт\n"
        "    Возврат Неопределено;\n"
        "КонецФункции",
        encoding="utf-8",
    )
    _metadata(extension / "DataProcessors" / "Печать.xml", "DataProcessor", "Печать")
    extension_module = extension / "DataProcessors" / "Печать" / "Ext" / "ObjectModule.bsl"
    extension_module.parent.mkdir(parents=True)
    extension_module.write_text(
        "Процедура Сформировать()\n"
        '    Область = ПечатьДокументовУНФ.ПолучитьОбластьБезопасно(Макет, "Шапка");\n'
        "КонецПроцедуры",
        encoding="utf-8",
    )
    context_path = tmp_path / "extension-context.json"

    with _session() as session:
        catalog = ProjectCatalog(session)
        base_project = catalog.add_project("TMS_UNF", base)
        catalog.add_project("TMS_EXT", extension)
        analyzer = AnalyzerCore(session)
        analyzer.analyze_project("TMS_UNF")
        analyzer.analyze_project("TMS_EXT")
        resolution = analyzer.resolve_project("TMS_EXT", include=("TMS_UNF",))
        call = session.scalar(
            select(CallSite).where(
                CallSite.project_id == catalog.get_project("TMS_EXT").id,
                CallSite.full_name == "ПечатьДокументовУНФ.ПолучитьОбластьБезопасно",
            )
        )
        assert call is not None
        target = session.get(Symbol, call.resolved_symbol_id)
        assert target is not None

        service = RetrievalService(session)
        outgoing = service.calls(
            "TMS_EXT",
            "Сформировать",
            direction="outgoing",
            include=("TMS_UNF",),
            depth=1,
        )
        incoming = service.calls(
            "TMS_UNF",
            "ПечатьДокументовУНФ.ПолучитьОбластьБезопасно",
            direction="incoming",
            include=("TMS_EXT",),
            depth=1,
        )
        impact = service.impact(
            "TMS_UNF",
            "ПечатьДокументовУНФ.ПолучитьОбластьБезопасно",
            include=("TMS_EXT",),
            depth=1,
        )
        written = service.context(
            "TMS_EXT",
            "Сформировать",
            context_path,
            include=("TMS_UNF",),
            depth=1,
            max_chars=20_000,
            max_units=10,
        )

    assert resolution.unresolved == 0
    assert call.resolution == "included_qualified_module"
    assert target.project_id == base_project.id
    assert any(row.callee_project == "TMS_UNF" for row in outgoing)
    assert any(row.project == "TMS_EXT" for row in incoming)
    assert any(row.project == "TMS_EXT" and row.relation == "calls" for row in impact)
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert {item["project"] for item in payload["source_units"]} == {"TMS_EXT", "TMS_UNF"}
    assert any(item["callee_project"] == "TMS_UNF" for item in payload["calls"])


def test_cross_project_context_prioritizes_primary_project_callers(tmp_path: Path) -> None:
    base = tmp_path / "base"
    extension = tmp_path / "extension"
    _metadata(base / "CommonModules" / "ОбщийAPI.xml", "CommonModule", "ОбщийAPI")
    base_module = base / "CommonModules" / "ОбщийAPI" / "Ext" / "Module.bsl"
    base_module.parent.mkdir(parents=True)
    base_module.write_text(
        "Функция ПолучитьДанные() Экспорт\n"
        "    Возврат 1;\n"
        "КонецФункции\n\n"
        "Процедура ТиповойВызывающийМетод() Экспорт\n"
        "    Данные = ОбщийAPI.ПолучитьДанные();\n"
        f'    Текст = "{"ОченьДлинныйТиповойКод" * 100}";\n'
        "КонецПроцедуры",
        encoding="utf-8",
    )
    _metadata(extension / "DataProcessors" / "Дополнение.xml", "DataProcessor", "Дополнение")
    extension_module = extension / "DataProcessors" / "Дополнение" / "Ext" / "ObjectModule.bsl"
    extension_module.parent.mkdir(parents=True)
    extension_module.write_text(
        "Процедура РасширенныйВызывающийМетод()\n"
        "    Данные = ОбщийAPI.ПолучитьДанные();\n"
        "КонецПроцедуры",
        encoding="utf-8",
    )
    context_path = tmp_path / "cross-project-priority.json"

    with _session() as session:
        catalog = ProjectCatalog(session)
        catalog.add_project("TMS_UNF", base)
        catalog.add_project("TMS_EXT", extension)
        analyzer = AnalyzerCore(session)
        analyzer.analyze_project("TMS_UNF")
        analyzer.analyze_project("TMS_EXT")
        analyzer.resolve_project("TMS_EXT", include=("TMS_UNF",))
        written = RetrievalService(session).context(
            "TMS_EXT",
            "ОбщийAPI.ПолучитьДанные",
            context_path,
            include=("TMS_UNF",),
            depth=1,
            max_chars=500,
            max_units=10,
        )

    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["source_units"][0]["symbol"] == "ОбщийAPI.ПолучитьДанные"
    assert payload["source_units"][1]["project"] == "TMS_EXT"
    assert "РасширенныйВызывающийМетод" in payload["source_units"][1]["source"]
