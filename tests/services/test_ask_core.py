"""Natural-language planning and reproducible LLM run tests."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from open1c_analyzer.config import Settings
from open1c_analyzer.services.analyzer import AnalyzerCore
from open1c_analyzer.services.ask import (
    AskService,
    ProviderResult,
    QuestionPlanner,
    ReasoningEffort,
)
from open1c_analyzer.services.project_catalog import ProjectCatalog
from open1c_analyzer.services.retrieval import RetrievalService
from open1c_analyzer.storage.models import Base, Module, Symbol


@contextmanager
def _session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def _metadata(path: Path, tag: str, name: str, synonym: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<MetaDataObject>"
        f"<{tag}><Properties><Name>{name}</Name><Synonym>{synonym}</Synonym>"
        f"</Properties></{tag}>"
        "</MetaDataObject>",
        encoding="utf-8",
    )


def _projects(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "base"
    extension = tmp_path / "extension"
    _metadata(
        base / "CommonModules" / "ПечатьДокументовУНФ.xml",
        "CommonModule",
        "ПечатьДокументовУНФ",
    )
    base_module = base / "CommonModules" / "ПечатьДокументовУНФ" / "Ext" / "Module.bsl"
    base_module.parent.mkdir(parents=True)
    base_module.write_text(
        "Функция ПолучитьОбластьБезопасно(Макет, ИмяОбласти) Экспорт\n"
        "    Возврат Макет.ПолучитьОбласть(ИмяОбласти);\n"
        "КонецФункции",
        encoding="utf-8",
    )
    _metadata(
        extension / "DataProcessors" / "ПечатьСчетНаОплату.xml",
        "DataProcessor",
        "ПечатьСчетНаОплату",
        "Печать счета на оплату",
    )
    _metadata(
        extension / "Enums" / "ДоступныеПечатныеФормыВыставлениеСчетов.xml",
        "Enum",
        "ДоступныеПечатныеФормыВыставлениеСчетов",
        "Доступные печатные формы выставления счетов на оплату",
    )
    extension_module = (
        extension / "DataProcessors" / "ПечатьСчетНаОплату" / "Ext" / "ManagerModule.bsl"
    )
    extension_module.parent.mkdir(parents=True)
    extension_module.write_text(
        '&Вместо("СформироватьПФ")\n'
        "Функция МКО_СформироватьПФ(Макет)\n"
        '    Возврат ПечатьДокументовУНФ.ПолучитьОбластьБезопасно(Макет, "Шапка");\n'
        "КонецФункции",
        encoding="utf-8",
    )
    return base, extension


class _FakeProvider:
    def __init__(self) -> None:
        self.input_text = ""

    def answer(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        reasoning_effort: ReasoningEffort,
        max_output_tokens: int,
    ) -> ProviderResult:
        assert model == "gpt-test"
        assert reasoning_effort == "low"
        assert max_output_tokens == 500
        assert "[PROJECT:relative/path.bsl" in instructions
        self.input_text = input_text
        return ProviderResult(
            "Расширение заменяет процедуру печати "
            "[TMS_EXT:DataProcessors/ПечатьСчетНаОплату/Ext/ManagerModule.bsl:1-4].",
            "resp-test",
            "req-test",
            100,
            25,
            125,
        )


def test_question_planner_selects_extension_print_object(tmp_path: Path) -> None:
    base, extension = _projects(tmp_path)

    with _session() as session:
        catalog = ProjectCatalog(session)
        catalog.add_project("TMS_UNF", base)
        catalog.add_project("TMS_EXT", extension)
        analyzer = AnalyzerCore(session)
        analyzer.analyze_project("TMS_UNF")
        analyzer.analyze_project("TMS_EXT", include=("TMS_UNF",))
        plan = QuestionPlanner(RetrievalService(session)).plan(
            "TMS_EXT",
            "Где и как расширение изменяет печатную форму счета на оплату?",
            include=("TMS_UNF",),
        )

    assert plan.selected_term == ("Обработка.ПечатьСчетНаОплату.manager_module.МКО_СформироватьПФ")
    assert plan.matches[0].entity.project == "TMS_EXT"
    assert plan.matches[0].entity.kind == "symbol"
    assert "directive=Вместо" in plan.matches[0].entity.details
    assert any("счет" in term for term in plan.search_terms)


def test_ask_dry_run_writes_plan_context_and_prompt(tmp_path: Path) -> None:
    base, extension = _projects(tmp_path)
    output_root = tmp_path / "runs"

    with _session() as session:
        catalog = ProjectCatalog(session)
        catalog.add_project("TMS_UNF", base)
        catalog.add_project("TMS_EXT", extension)
        analyzer = AnalyzerCore(session)
        analyzer.analyze_project("TMS_UNF")
        analyzer.analyze_project("TMS_EXT", include=("TMS_UNF",))
        result = AskService(
            session,
            settings=Settings(runs_path=output_root),
        ).ask(
            "TMS_EXT",
            "Где расширение меняет печать счета на оплату?",
            include=("TMS_UNF",),
            dry_run=True,
            max_chars=20_000,
            max_prompt_chars=50_000,
        )

    assert result.dry_run is True
    assert result.context_path.exists()
    assert result.prompt_path.exists()
    assert result.answer_path is None
    context = json.loads(result.context_path.read_text(encoding="utf-8"))
    run = json.loads((result.run_directory / "run.json").read_text(encoding="utf-8"))
    request = json.loads((result.run_directory / "request.json").read_text(encoding="utf-8"))
    assert context["ask"]["question"].startswith("Где расширение")
    assert context["ask"]["plan"]["selected_term"].endswith("МКО_СформироватьПФ")
    assert context["source_units"][0]["project"] == "TMS_EXT"
    assert context["source_units"][0]["symbol"].endswith("МКО_СформироватьПФ")
    assert {item["project"] for item in context["source_units"]} == {"TMS_EXT", "TMS_UNF"}
    assert context["calls"]
    assert run["status"] == "prepared"
    assert request["context_sha256"]
    assert "КОНТЕКСТ OPEN1C ANALYZER" in result.prompt_path.read_text(encoding="utf-8")


def test_ask_with_provider_persists_answer_and_usage(tmp_path: Path) -> None:
    base, extension = _projects(tmp_path)
    provider = _FakeProvider()

    with _session() as session:
        catalog = ProjectCatalog(session)
        catalog.add_project("TMS_UNF", base)
        catalog.add_project("TMS_EXT", extension)
        analyzer = AnalyzerCore(session)
        analyzer.analyze_project("TMS_UNF")
        analyzer.analyze_project("TMS_EXT", include=("TMS_UNF",))
        result = AskService(session, provider=provider).ask(
            "TMS_EXT",
            "Как изменена печать счета на оплату?",
            include=("TMS_UNF",),
            term="Обработка.ПечатьСчетНаОплату",
            model="gpt-test",
            reasoning_effort="low",
            max_output_tokens=500,
            max_chars=20_000,
            max_prompt_chars=50_000,
            output_root=tmp_path / "runs",
        )

    assert result.answer_path is not None and result.answer_path.exists()
    assert result.request_id == "req-test"
    assert result.total_tokens == 125
    assert "ПечатьСчетНаОплату" in provider.input_text
    run = json.loads((result.run_directory / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "completed"
    assert run["provider"]["response_id"] == "resp-test"


def test_openai_provider_uses_responses_api_without_remote_storage(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    import sys
    from types import ModuleType, SimpleNamespace

    from open1c_analyzer.services.ask import OpenAIResponsesProvider

    captured: dict[str, object] = {}

    class _Responses:
        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return SimpleNamespace(
                output_text="Ответ",
                id="resp-1",
                _request_id="req-1",
                usage=SimpleNamespace(input_tokens=10, output_tokens=3, total_tokens=13),
            )

    class _Client:
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            captured["client"] = kwargs
            self.responses = _Responses()

    module = ModuleType("openai")
    module.OpenAI = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", module)

    provider = OpenAIResponsesProvider(Settings(openai_api_key="secret"))
    result = provider.answer(
        model="gpt-5.6",
        instructions="Инструкции",
        input_text="Контекст",
        reasoning_effort="medium",
        max_output_tokens=500,
    )

    assert captured["model"] == "gpt-5.6"
    assert captured["reasoning"] == {"effort": "medium"}
    assert captured["store"] is False
    assert result.request_id == "req-1"
    assert result.total_tokens == 13


def test_question_planner_prioritizes_document_list_form_and_context(tmp_path: Path) -> None:
    base = tmp_path / "base"
    extension = tmp_path / "extension"
    _metadata(base / "Documents" / "РасходнаяНакладная.xml", "Document", "РасходнаяНакладная")
    _metadata(
        extension / "Documents" / "РасходнаяНакладная.xml",
        "Document",
        "РасходнаяНакладная",
    )

    base_form = (
        base / "Documents" / "РасходнаяНакладная" / "Forms" / "ФормаСписка" / "Ext" / "Form.xml"
    )
    base_form.parent.mkdir(parents=True)
    base_form.write_text(
        """<Form xmlns:v8="urn:test" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<Events><Event name="OnCreateAtServer">ПриСозданииНаСервере</Event></Events>
<ChildItems><Table name="Список" id="1"><ChildItems/></Table></ChildItems>
<Attributes><Attribute name="Список" id="1"><Type><v8:Type>cfg:DynamicList</v8:Type></Type>
<Settings xsi:type="DynamicList">
<ManualQuery>true</ManualQuery><DynamicDataRead>true</DynamicDataRead>
<QueryText>ВЫБРАТЬ\n ДокументРасходнаяНакладная.Ссылка КАК Ссылка\nИЗ\n
 Документ.РасходнаяНакладная КАК ДокументРасходнаяНакладная</QueryText>
<MainTable>Document.РасходнаяНакладная</MainTable></Settings></Attribute></Attributes>
</Form>""",
        encoding="utf-8",
    )
    base_module = base_form.parent / "Form" / "Module.bsl"
    base_module.parent.mkdir(parents=True)
    base_module.write_text(
        "&НаСервере\nПроцедура ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)\nКонецПроцедуры",
        encoding="utf-8",
    )

    extension_form = (
        extension
        / "Documents"
        / "РасходнаяНакладная"
        / "Forms"
        / "ФормаСписка"
        / "Ext"
        / "Form.xml"
    )
    extension_form.parent.mkdir(parents=True)
    extension_form.write_text(
        (
            '<Form><Events><Event name="OnCreateAtServer" callType="After">'
            "МКО_ПриСозданииНаСервереПосле</Event></Events></Form>"
        ),
        encoding="utf-8",
    )
    extension_module = extension_form.parent / "Form" / "Module.bsl"
    extension_module.parent.mkdir(parents=True)
    extension_module.write_text(
        "&НаСервере\n"
        "Процедура МКО_ПриСозданииНаСервереПосле(Отказ, СтандартнаяОбработка)\n"
        "    ТекстЗапроса = ЭтаФорма.Список.ТекстЗапроса;\n"
        "КонецПроцедуры",
        encoding="utf-8",
    )
    manager = extension / "Documents" / "РасходнаяНакладная" / "Ext" / "ManagerModule.bsl"
    manager.parent.mkdir(parents=True, exist_ok=True)
    manager.write_text(
        "Процедура СформироватьПечатнуюФорму()\nКонецПроцедуры",
        encoding="utf-8",
    )

    with _session() as session:
        catalog = ProjectCatalog(session)
        catalog.add_project("TMS_UNF", base)
        catalog.add_project("TMS_EXT", extension)
        analyzer = AnalyzerCore(session)
        analyzer.analyze_project("TMS_UNF")
        analyzer.analyze_project("TMS_EXT", include=("TMS_UNF",))
        planner = QuestionPlanner(RetrievalService(session))
        plan = planner.plan(
            "TMS_EXT",
            "Где и как изменить форму списка документа РасходнаяНакладная, чтобы добавить колонку?",
            include=("TMS_UNF",),
        )
        payload = RetrievalService(session).build_context(
            "TMS_EXT",
            plan.selected_term,
            include=("TMS_UNF",),
            depth=2,
            max_chars=30_000,
            max_units=10,
        )

    assert plan.selected_term == "Документ.РасходнаяНакладная.Form.ФормаСписка"
    assert payload["source_units"][0]["unit_kind"] == "metadata"
    assert payload["source_units"][0]["project"] == "TMS_EXT"
    assert payload["source_units"][0]["file"].endswith("Forms/ФормаСписка/Ext/Form.xml")
    assert any(
        unit.get("unit_kind") == "symbol"
        and unit["project"] == "TMS_EXT"
        and "МКО_ПриСозданииНаСервереПосле" in unit["symbol"]
        for unit in payload["source_units"]
    )
    assert any("<QueryText>" in unit["source"] for unit in payload["source_units"])


@pytest.mark.parametrize("suffix", ["", " Покажи список процедур записи."])
def test_question_planner_prioritizes_named_register_for_writer_question(
    tmp_path: Path, suffix: str
) -> None:
    base = tmp_path / "base-register"
    extension = tmp_path / "extension-register"
    _metadata(
        base / "InformationRegisters" / "СуммыДокументовРегламентированныйУчет.xml",
        "InformationRegister",
        "СуммыДокументовРегламентированныйУчет",
    )
    _metadata(base / "Documents" / "РасходнаяНакладная.xml", "Document", "РасходнаяНакладная")
    form = base / "Documents" / "РасходнаяНакладная" / "Forms" / "ФормаСписка" / "Ext" / "Form.xml"
    form.parent.mkdir(parents=True)
    form.write_text("<Form><Events/></Form>", encoding="utf-8")
    _metadata(
        extension / "Documents" / "ЗаказПокупателя.xml",
        "Document",
        "ЗаказПокупателя",
    )
    distractor = extension / "Documents" / "ЗаказПокупателя" / "Ext" / "ObjectModule.bsl"
    distractor.parent.mkdir(parents=True)
    distractor.write_text(
        '&После("ПередЗаписью")\n'
        "Процедура МКО_ПередЗаписью(Отказ)\n"
        "    ЭтотОбъект.Комментарий = ТекущаяДатаСеанса();\n"
        "КонецПроцедуры",
        encoding="utf-8",
    )

    question = (
        "Как формируется регистр сведений СуммыДокументовРегламентированныйУчет "
        "для документа РасходнаяНакладная? Найди именно запись регистра, а не только "
        "его чтение. Проследи цепочку до фактической записи регистра и покажи, откуда "
        "берется ресурс Всего."
    )

    with _session() as session:
        catalog = ProjectCatalog(session)
        catalog.add_project("TMS_UNF", base)
        catalog.add_project("TMS_EXT", extension)
        analyzer = AnalyzerCore(session)
        analyzer.analyze_project("TMS_UNF")
        analyzer.analyze_project("TMS_EXT")
        plan = QuestionPlanner(RetrievalService(session)).plan(
            "TMS_UNF",
            question + suffix,
            include=("TMS_EXT",),
        )

    assert plan.selected_term == "РегистрСведений.СуммыДокументовРегламентированныйУчет"
    assert "СуммыДокументовРегламентированныйУчет" in plan.search_terms[:4]
    assert plan.matches[0].entity.kind == "metadata"
    assert "kind=information_register" in plan.matches[0].entity.details


@pytest.mark.parametrize(
    "question",
    [
        "Как формируется регистр? Покажи список процедур записи.",
        "Где формируются движения? Нужен список методов.",
        "Как сформировать список вызовов?",
    ],
)
def test_formation_verbs_and_procedure_lists_are_not_list_form_intent(question: str) -> None:
    assert not QuestionPlanner._form_list_intent(question)


@pytest.mark.parametrize(
    "question",
    [
        "Как изменить форму списка?",
        "Где обработчик формы списка?",
        "Как работать с формой списка?",
        "Добавить колонку в ФормаСписка",
    ],
)
def test_list_form_intent_preserves_explicit_form_mentions(question: str) -> None:
    assert QuestionPlanner._form_list_intent(question)


def _register_writer_projects(session: Session, root: Path) -> None:
    base, extension = root / "base", root / "extension"
    _metadata(
        base / "InformationRegisters" / "СуммыДокументовРегламентированныйУчет.xml",
        "InformationRegister",
        "СуммыДокументовРегламентированныйУчет",
    )
    for source, name in ((base, "РасходнаяНакладная"), (extension, "ЗаказПокупателя")):
        _metadata(source / "Documents" / f"{name}.xml", "Document", name)
        module = source / "Documents" / name / "Ext" / "ObjectModule.bsl"
        module.parent.mkdir(parents=True)
        module.write_text(
            "Процедура ОбработкаПроведения(Отказ)\n"
            + ("Документы.РасходнаяНакладная.СформироватьДанные();\n" if source == base else "")
            + "ПроведениеДокументовУНФ.ОтразитьДвижения("
            '"СуммыДокументовРегламентированныйУчет", Данные);\n'
            "КонецПроцедуры",
            encoding="utf-8",
        )
    manager = base / "Documents" / "РасходнаяНакладная" / "Ext" / "ManagerModule.bsl"
    manager.write_text(
        "Процедура СформироватьДанные() Экспорт\n"
        "АПосторонняя();\nРассчитатьРесурс();\nКонецПроцедуры\n"
        "Процедура АПосторонняя()\nСлужебный.Флаг = Истина;\nКонецПроцедуры\n"
        "Процедура РассчитатьРесурс()\nСтрока.Всего = Данные.СуммаРег;\nКонецПроцедуры",
        encoding="utf-8",
    )
    catalog = ProjectCatalog(session)
    catalog.add_project("Base", base)
    catalog.add_project("Extension", extension)
    analyzer = AnalyzerCore(session)
    analyzer.analyze_project("Base")
    analyzer.analyze_project("Extension", include=("Base",))


def test_register_context_prefers_writer_of_explicit_document(tmp_path: Path) -> None:
    with _session() as session:
        _register_writer_projects(session, tmp_path)
        retrieval = RetrievalService(session)
        options = {"include": ("Base",), "max_units": 1}
        generic = retrieval.build_context(
            "Extension", "РегистрСведений.СуммыДокументовРегламентированныйУчет", **options
        )
        focused = retrieval.build_context(
            "Extension",
            "РегистрСведений.СуммыДокументовРегламентированныйУчет",
            question="Запись регистра при проведении документа РасходнаяНакладная",
            **options,
        )
    assert "ЗаказПокупателя" in generic["source_units"][0]["symbol"]
    assert (
        focused["source_units"][0]["symbol"]
        == "Документ.РасходнаяНакладная.object_module.ОбработкаПроведения"
    )


def test_context_search_terms_rank_procedure_body_before_location(tmp_path: Path) -> None:
    with _session() as session:
        _register_writer_projects(session, tmp_path)
        retrieval = RetrievalService(session)
        generic = retrieval.build_context("Base", "СформироватьДанные", depth=2)
        focused = retrieval.build_context(
            "Base", "СформироватьДанные", depth=2, search_terms=("Всего", "СуммаРег")
        )
    generic_names = [unit["symbol"].rsplit(".", 1)[-1] for unit in generic["source_units"]]
    focused_names = [unit["symbol"].rsplit(".", 1)[-1] for unit in focused["source_units"]]
    assert generic_names.index("АПосторонняя") < generic_names.index("РассчитатьРесурс")
    assert focused_names.index("РассчитатьРесурс") < focused_names.index("АПосторонняя")
    assert focused_names[0] == generic_names[0] == "СформироватьДанные"


def test_ask_register_writer_document_manager_and_data_flow(tmp_path: Path) -> None:
    question = (
        "Как формируется регистр сведений СуммыДокументовРегламентированныйУчет "
        "при проведении документа РасходнаяНакладная? Откуда берется ресурс Всего и СуммаРег?"
    )
    with _session() as session:
        _register_writer_projects(session, tmp_path)
        result = AskService(session).ask(
            "Extension",
            question,
            include=("Base",),
            depth=3,
            max_units=3,
            dry_run=True,
            output_root=tmp_path / "runs",
        )
        payload = json.loads(result.context_path.read_text(encoding="utf-8"))
    assert payload["request"]["term"] == "РегистрСведений.СуммыДокументовРегламентированныйУчет"
    units = payload["source_units"]
    assert units[0]["symbol"] == "Документ.РасходнаяНакладная.object_module.ОбработкаПроведения"
    assert any("manager_module.СформироватьДанные" in unit["symbol"] for unit in units)
    assert any("manager_module.РассчитатьРесурс" in unit["symbol"] for unit in units)
    assert all("ЗаказПокупателя" not in unit["symbol"] for unit in units)
    assert any(
        edge["relation"] == "calls" and "manager_module.СформироватьДанные" in edge["target"]
        for edge in payload["impact"]
    )
    assert any(
        flow["target"] == "Строка.Всего" and "СуммаРег" in flow["expression"]
        for flow in payload["data_flow"]
    )
    assert "всего" in [term.casefold() for term in payload["request"]["search_terms"]]


def test_focused_writer_graph_reserves_budget_for_relevant_deeper_calls(tmp_path: Path) -> None:
    with _session() as session:
        _register_writer_projects(session, tmp_path)
        retrieval = RetrievalService(session)
        project = retrieval.catalog.get_project("Base")
        writer = session.scalars(
            select(Symbol)
            .join(Module, Module.id == Symbol.module_id)
            .where(
                Symbol.project_id == project.id,
                Symbol.name == "ОбработкаПроведения",
                Module.owner_name == "РасходнаяНакладная",
            )
        ).one()
        seeds = {(project.id, writer.id)}
        _, generic = retrieval._symbol_dependency_neighborhood(
            seeds, {project.id: project}, depth=2, limit=2
        )
        _, focused = retrieval._symbol_dependency_neighborhood(
            seeds, {project.id: project}, depth=2, limit=2, search_terms=("Ресурс",)
        )
    assert len(focused) == 2
    assert any(edge.target.endswith("АПосторонняя") for edge in generic)
    assert any(edge.target.endswith("РассчитатьРесурс") for edge in focused)
    assert all(not edge.target.endswith("АПосторонняя") for edge in focused)
