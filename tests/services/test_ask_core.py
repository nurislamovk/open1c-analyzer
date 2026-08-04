"""Natural-language planning and reproducible LLM run tests."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
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
