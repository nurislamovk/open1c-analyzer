"""Resolution, focused retrieval, impact analysis and LLM context tests."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from open1c_analyzer.services.analyzer import AnalyzerCore
from open1c_analyzer.services.ask import AskService, QuestionPlanner
from open1c_analyzer.services.project_catalog import ProjectCatalog
from open1c_analyzer.services.retrieval import RetrievalService
from open1c_analyzer.storage.models import Base, CallSite, Module, Reference, Symbol


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


@pytest.mark.parametrize("limit", [1, 20])
@pytest.mark.parametrize("lowercase", [False, True])
def test_qualified_symbol_lookup_filters_owner_before_limit(
    tmp_path: Path, limit: int, lowercase: bool
) -> None:
    source = _configuration(tmp_path)
    for index in range(85):
        name = f"Шум{index}"
        _metadata(source / "CommonModules" / f"{name}.xml", "CommonModule", name)
        distractor = source / "CommonModules" / name / "Ext" / "Module.bsl"
        distractor.parent.mkdir(parents=True)
        distractor.write_text(
            "Процедура ОбработкаПроведения() Экспорт\nКонецПроцедуры", encoding="utf-8"
        )
    term = "Документ.Заказ.object_module.ОбработкаПроведения"
    with _session() as session:
        catalog = ProjectCatalog(session)
        catalog.add_project("Base", source)
        extension = tmp_path / "extension"
        extension.mkdir()
        catalog.add_project("Extension", extension)
        AnalyzerCore(session).analyze_project("Base")
        retrieval = RetrievalService(session)
        for project, include in (("Base", ()), ("Extension", ("Base",))):
            matches = retrieval.find(
                project, term.lower() if lowercase else term, include=include, limit=limit
            )
            assert [item.name for item in matches] == [term]
            assert (
                retrieval.find(
                    project,
                    "Документ.Несуществующий.object_module.ОбработкаПроведения",
                    include=include,
                    limit=limit,
                )
                == []
            )
            payload = retrieval.build_context(project, term, include=include, max_units=1)
            assert payload["source_units"][0]["symbol"] == term
            assert "Процедура ОбработкаПроведения" in payload["source_units"][0]["source"]


def _posting_configuration(root: Path, *, graph: bool = True) -> Path:
    source = root / "posting"
    for name in ("Отгрузка", "ЧужойДокумент"):
        _metadata(source / "Documents" / f"{name}.xml", "Document", name)
        module = source / "Documents" / name / "Ext" / "ObjectModule.bsl"
        module.parent.mkdir(parents=True)
        module.write_text(
            "Процедура ОбработкаПроведения(Отказ)\n"
            + (f"Документы.{name}.РассчитатьПроведение();\n" if graph else "")
            + "КонецПроцедуры",
            encoding="utf-8",
        )
        manager = module.with_name("ManagerModule.bsl")
        manager.write_text(
            "Процедура РассчитатьПроведение() Экспорт\n"
            "Строка.Всего = Данные.Сумма;\nКонецПроцедуры\n"
            + "\n".join(
                f"Процедура ПечатьПроведения{index}() Экспорт\n"
                f'Текст = "Проведение {"шум" * 1200}";\nКонецПроцедуры'
                for index in range(35)
            ),
            encoding="utf-8",
        )
    return source


@pytest.mark.parametrize("included", [False, True])
def test_posting_context_reserves_graph_backed_handler_before_wide_metadata_seeds(
    tmp_path: Path, included: bool
) -> None:
    source = _posting_configuration(tmp_path)
    with _session() as session:
        catalog = ProjectCatalog(session)
        catalog.add_project("Base", source)
        extension = tmp_path / "extension"
        extension.mkdir()
        catalog.add_project("Extension", extension)
        AnalyzerCore(session).analyze_project("Base")
        payload = RetrievalService(session).build_context(
            "Extension" if included else "Base",
            "Документ.Отгрузка",
            include=("Base",) if included else (),
            question="Как проводится документ Отгрузка? Покажи обработчик проведения.",
            search_terms=("Отгрузка", "проведения"),
            depth=2,
        )
    units = payload["source_units"]
    assert units[0]["symbol"] == "Документ.Отгрузка.object_module.ОбработкаПроведения"
    assert any(unit["symbol"].endswith("manager_module.РассчитатьПроведение") for unit in units)
    assert all("ЧужойДокумент" not in unit["symbol"] for unit in units)
    assert payload["limits"]["actual_source_characters"] <= 60_000
    assert len(units) <= 30
    assert any(flow["target"] == "Строка.Всего" for flow in payload["data_flow"])
    assert any(
        edge["source"].endswith("object_module.ОбработкаПроведения")
        and edge["target"].endswith("manager_module.РассчитатьПроведение")
        for edge in payload["impact"]
    )


@pytest.mark.parametrize(
    ("graph", "question"),
    [
        (False, "Как проводится документ Отгрузка?"),
        (True, "Как печатается документ Отгрузка?"),
        (True, "Как печатается документ Отгрузка из проводника?"),
    ],
)
def test_posting_handler_is_not_promoted_without_intent_and_graph_evidence(
    tmp_path: Path, graph: bool, question: str
) -> None:
    source = _posting_configuration(tmp_path, graph=graph)
    with _session() as session:
        ProjectCatalog(session).add_project("Base", source)
        AnalyzerCore(session).analyze_project("Base")
        payload = RetrievalService(session).build_context(
            "Base", "Документ.Отгрузка", question=question, max_units=1
        )
    assert payload["source_units"][0]["symbol"] != (
        "Документ.Отгрузка.object_module.ОбработкаПроведения"
    )


@pytest.mark.parametrize("include", [("Base",), ("Base", "Extension", "Base")])
def test_qualified_lookup_and_context_keep_extension_ahead_of_base(
    tmp_path: Path, include: tuple[str, ...]
) -> None:
    with _session() as session:
        catalog = ProjectCatalog(session)
        analyzer = AnalyzerCore(session)
        for project in ("Base", "Extension"):
            root = tmp_path / project
            source = _configuration(root)
            if project == "Base":
                module = source / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl"
                module.write_text(
                    module.read_text(encoding="utf-8").replace(
                        "КонецПроцедуры", "Строка.Всего = Данные.Сумма;\nКонецПроцедуры"
                    ),
                    encoding="utf-8",
                )
            catalog.add_project(project, source)
            analyzer.analyze_project(project)
        retrieval = RetrievalService(session)
        term = "Документ.Заказ.object_module.ОбработкаПроведения"
        matches = retrieval.find("Extension", term, include=include, limit=1)
        payload = retrieval.build_context(
            "Extension", term, include=include, max_units=1, search_terms=("Всего",)
        )
        plan = QuestionPlanner(retrieval).plan(
            "Extension", "Как проводится документ Заказ?", include=include
        )
    assert [match.project for match in matches] == ["Extension"]
    assert payload["source_units"][0]["project"] == "Extension"
    assert payload["source_units"][0]["symbol"] == term
    assert plan.matches[0].entity.project == "Extension"
    assert plan.selected_term == term


def test_ask_posting_question_keeps_handler_manager_and_flow(tmp_path: Path) -> None:
    source = _posting_configuration(tmp_path)
    with _session() as session:
        ProjectCatalog(session).add_project("Demo", source)
        AnalyzerCore(session).analyze_project("Demo")
        result = AskService(session).ask(
            "Demo",
            "Как проводится документ Отгрузка? "
            "Покажи обработчик проведения и вызываемые процедуры.",
            dry_run=True,
            output_root=tmp_path / "runs",
        )
    payload = json.loads(result.context_path.read_text(encoding="utf-8"))
    assert payload["source_units"][0]["symbol"] == (
        "Документ.Отгрузка.object_module.ОбработкаПроведения"
    )
    assert any(
        unit["symbol"] == "Документ.Отгрузка.manager_module.РассчитатьПроведение"
        for unit in payload["source_units"]
    )
    assert any(flow["target"] == "Строка.Всего" for flow in payload["data_flow"])


def test_posting_intent_does_not_replace_explicit_manager_procedure(tmp_path: Path) -> None:
    source = _posting_configuration(tmp_path)
    term = "Документ.Отгрузка.manager_module.РассчитатьПроведение"
    with _session() as session:
        ProjectCatalog(session).add_project("Demo", source)
        AnalyzerCore(session).analyze_project("Demo")
        result = AskService(session).ask(
            "Demo",
            f"Как работает {term} при проведении документа Отгрузка?",
            dry_run=True,
            output_root=tmp_path / "runs",
        )
    payload = json.loads(result.context_path.read_text(encoding="utf-8"))
    assert payload["request"]["term"] == term
    assert payload["source_units"][0]["symbol"] == term


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


def test_register_writer_dependencies_expand_to_calculation_and_data_flow(tmp_path: Path) -> None:
    source = tmp_path / "configuration"
    _metadata(
        source / "InformationRegisters" / "СуммыДокументовРегламентированныйУчет.xml",
        "InformationRegister",
        "СуммыДокументовРегламентированныйУчет",
    )
    _metadata(
        source / "CommonModules" / "РасчетыПроведениеДокументов.xml",
        "CommonModule",
        "РасчетыПроведениеДокументов",
    )
    _metadata(source / "Documents" / "РасходнаяНакладная.xml", "Document", "РасходнаяНакладная")

    common = source / "CommonModules" / "РасчетыПроведениеДокументов" / "Ext" / "Module.bsl"
    common.parent.mkdir(parents=True)
    common.write_text(
        "Процедура СформироватьТаблицаСуммыДокументовРегламентированныйУчет() Экспорт\n"
        "    СтрокаДвижения.Всего = СтрокаСуммВНацВалюте.СуммаРег;\n"
        "    СтрокаДвижения.НДС = СтрокаСуммВНацВалюте.СуммаНДСРег;\n"
        "КонецПроцедуры",
        encoding="utf-8",
    )
    document = source / "Documents" / "РасходнаяНакладная" / "Ext" / "ObjectModule.bsl"
    document.parent.mkdir(parents=True)
    document.write_text(
        "Процедура ОбработкаПроведения(Отказ)\n"
        "    РасчетыПроведениеДокументов."
        "СформироватьТаблицаСуммыДокументовРегламентированныйУчет();\n"
        "    ПроведениеДокументовУНФ.ОтразитьДвижения("
        '"СуммыДокументовРегламентированныйУчет", ТаблицыДляДвижений, Движения, Отказ);\n'
        "КонецПроцедуры",
        encoding="utf-8",
    )

    with _session() as session:
        ProjectCatalog(session).add_project("Demo", source)
        analysis = AnalyzerCore(session).analyze_project("Demo")
        payload = RetrievalService(session).build_context(
            "Demo",
            "РегистрСведений.СуммыДокументовРегламентированныйУчет",
            depth=3,
            max_chars=30_000,
            max_units=20,
        )

    assert analysis.errors == ()
    assert any(item["relation"] == "register_write" for item in payload["writes"])
    assert any(
        "ОбработкаПроведения" in item["symbol"]
        for item in payload["source_units"]
        if item.get("unit_kind") == "symbol"
    )
    assert any(
        "СформироватьТаблицаСуммыДокументовРегламентированныйУчет" in item["symbol"]
        for item in payload["source_units"]
        if item.get("unit_kind") == "symbol"
    )
    assert any(
        item["target"] == "СтрокаДвижения.Всего"
        and "СтрокаСуммВНацВалюте.СуммаРег" in item["expression"]
        for item in payload["data_flow"]
    )


@pytest.mark.parametrize("extension_manager", [False, True])
def test_document_manager_calls_require_exact_document_export(
    tmp_path: Path, extension_manager: bool
) -> None:
    base, extension = tmp_path / "base", tmp_path / "extension"
    for source in (base, extension):
        _metadata(source / "Documents" / "Накладная.xml", "Document", "Накладная")
    manager = base / "Documents" / "Накладная" / "Ext" / "ManagerModule.bsl"
    manager.parent.mkdir(parents=True)
    manager.write_text(
        "Процедура Расчет() Экспорт\nКонецПроцедуры\nПроцедура Закрытая()\nКонецПроцедуры",
        encoding="utf-8",
    )
    caller = extension / "Documents" / "Накладная" / "Ext" / "ObjectModule.bsl"
    caller.parent.mkdir(parents=True)
    caller.write_text(
        "Процедура Проверка()\n"
        "Документы.Накладная.Расчет();\n"
        "Documents.Накладная.Расчет();\n"
        "Документы.ДругойДокумент.Расчет();\n"
        "Документы.Накладная.Закрытая();\n"
        "Документы.Накладная.ТолькоОбъект();\n"
        "Документы.Накладная.СоздатьДокумент();\n"
        "КонецПроцедуры\n"
        "Процедура Расчет() Экспорт\nКонецПроцедуры\n"
        "Процедура ТолькоОбъект() Экспорт\nКонецПроцедуры",
        encoding="utf-8",
    )
    if extension_manager:
        caller.with_name("ManagerModule.bsl").write_text(
            manager.read_text(encoding="utf-8"), encoding="utf-8"
        )
    with _session() as session:
        catalog = ProjectCatalog(session)
        primary = catalog.add_project("Extension", extension)
        included = catalog.add_project("Base", base)
        analyzer = AnalyzerCore(session)
        analyzer.analyze_project("Base")
        analyzer.analyze_project("Extension", include=("Base",))
        calls = list(
            session.scalars(
                select(CallSite).where(CallSite.project_id == primary.id).order_by(CallSite.line)
            )
        )
        for call in calls[:2]:
            target = session.get(Symbol, call.resolved_symbol_id)
            assert target is not None
            target_module = session.get(Module, target.module_id)
            assert target_module is not None
            assert target_module.module_kind == "manager_module"
            assert target_module.owner_kind == "document"
            assert target_module.owner_name == "Накладная"
            assert target.is_export
            assert target.project_id == (primary.id if extension_manager else included.id)
            assert call.resolution == (
                "document_manager" if extension_manager else "included_document_manager"
            )
        assert all(call.resolved_symbol_id is None for call in calls[2:])
        assert calls[-1].resolution == "platform_api"


def test_analysis_schema_upgrade_removes_stale_register_writer_facts(tmp_path: Path) -> None:
    source = tmp_path / "configuration"
    _metadata(source / "Documents" / "Проверка.xml", "Document", "Проверка")
    _metadata(source / "InformationRegisters" / "Контроль.xml", "InformationRegister", "Контроль")
    module = source / "Documents" / "Проверка" / "Ext" / "ObjectModule.bsl"
    module.parent.mkdir(parents=True)
    module.write_text(
        "Процедура Прочитать()\nКоличество = Движения.Контроль.Количество();\nКонецПроцедуры",
        encoding="utf-8",
    )
    with _session() as session:
        project = ProjectCatalog(session).add_project("Demo", source)
        analyzer = AnalyzerCore(session)
        analyzer.analyze_project("Demo")
        symbol = session.scalars(select(Symbol).where(Symbol.project_id == project.id)).one()
        # Simulate the cached false writer produced by schema 2, with unchanged files.
        project.profile_json = json.dumps({"Open1CAnalysisSchemaVersion": "2"})
        session.add(
            Reference(
                project_id=project.id,
                source_file_id=symbol.source_file_id,
                symbol_id=symbol.id,
                target_kind="register",
                target_name="Контроль",
                target_full_name="Контроль",
                normalized_target="контроль",
                relation="register_write",
                line=2,
            )
        )
        session.flush()
        analyzer.resolve_project("Demo")
        retrieval = RetrievalService(session)
        assert retrieval.build_context("Demo", "РегистрСведений.Контроль")["writes"]
        upgraded = analyzer.analyze_project("Demo")
        assert upgraded.errors == ()
        assert upgraded.analyzed_files == 3
        assert upgraded.graph_rebuilt
        assert not retrieval.build_context("Demo", "РегистрСведений.Контроль")["writes"]
        assert analyzer.analyze_project("Demo").analyzed_files == 0
