"""Question planning, bounded context preparation and OpenAI Responses integration."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol

from sqlalchemy.orm import Session

from open1c_analyzer.config import Settings
from open1c_analyzer.parser.names import normalize
from open1c_analyzer.services.project_catalog import ProjectCatalogError
from open1c_analyzer.services.retrieval import EntityRef, RetrievalService

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]


class AskError(RuntimeError):
    """A user-facing error raised by the LLM integration layer."""


@dataclass(frozen=True, slots=True)
class PlannedMatch:
    """One ranked knowledge-graph match selected from a natural-language question."""

    score: int
    matched_terms: tuple[str, ...]
    entity: EntityRef


@dataclass(frozen=True, slots=True)
class AskPlan:
    """Deterministic retrieval plan produced before any LLM request."""

    question: str
    search_terms: tuple[str, ...]
    selected_term: str
    matches: tuple[PlannedMatch, ...]


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Normalized response returned by an LLM provider."""

    text: str
    response_id: str | None
    request_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True, slots=True)
class AskResult:
    """Completed or prepared engineering question run."""

    run_directory: Path
    selected_term: str
    context_path: Path
    prompt_path: Path
    answer_path: Path | None
    answer: str | None
    model: str
    dry_run: bool
    request_id: str | None = None
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class LLMProvider(Protocol):
    """Small provider boundary used by the service and tests."""

    def answer(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        reasoning_effort: ReasoningEffort,
        max_output_tokens: int,
    ) -> ProviderResult:
        """Return one answer for a fully prepared, source-backed prompt."""


class OpenAIResponsesProvider:
    """OpenAI Responses API implementation using the official Python SDK."""

    def __init__(self, settings: Settings) -> None:
        if settings.openai_api_key is None:
            raise AskError(
                "OpenAI API key is not configured. Set OPENAI_API_KEY or OPEN1C_OPENAI_API_KEY."
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency is installed normally
            raise AskError("The openai package is not installed. Run uv sync --extra dev.") from exc

        self._client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
        )

    def answer(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        reasoning_effort: ReasoningEffort,
        max_output_tokens: int,
    ) -> ProviderResult:
        try:
            response = self._client.responses.create(
                model=model,
                instructions=instructions,
                input=input_text,
                reasoning={"effort": reasoning_effort},
                max_output_tokens=max_output_tokens,
                store=False,
            )
        except Exception as exc:  # OpenAI exposes several typed API exceptions
            request_id = getattr(exc, "request_id", None)
            suffix = f" Request ID: {request_id}." if request_id else ""
            raise AskError(f"OpenAI request failed: {exc}.{suffix}") from exc

        usage = getattr(response, "usage", None)
        return ProviderResult(
            text=str(response.output_text or "").strip(),
            response_id=str(response.id) if response.id else None,
            request_id=getattr(response, "_request_id", None),
            input_tokens=_optional_int(getattr(usage, "input_tokens", None)),
            output_tokens=_optional_int(getattr(usage, "output_tokens", None)),
            total_tokens=_optional_int(getattr(usage, "total_tokens", None)),
        )


class QuestionPlanner:
    """Resolve a natural-language engineering question to indexed 1C entities."""

    _word_re = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+", re.UNICODE)
    _quoted_re = re.compile(r"[\"«](.*?)[\"»]")
    _qualified_re = re.compile(
        r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"
        r"(?:\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)+"
    )
    _stopwords: ClassVar[set[str]] = {
        "а",
        "без",
        "бы",
        "в",
        "где",
        "для",
        "до",
        "его",
        "ее",
        "её",
        "и",
        "из",
        "или",
        "как",
        "каким",
        "какая",
        "какие",
        "когда",
        "который",
        "ли",
        "на",
        "над",
        "не",
        "но",
        "о",
        "об",
        "от",
        "по",
        "под",
        "при",
        "про",
        "с",
        "со",
        "что",
        "эта",
        "это",
        "этот",
        "the",
        "and",
        "for",
        "from",
        "how",
        "what",
        "where",
        "with",
    }
    _code_intent_words: ClassVar[set[str]] = {
        "анализ",
        "вызов",
        "вызывает",
        "где",
        "изменяет",
        "использует",
        "как",
        "меняет",
        "обрабатывает",
        "переопределяет",
        "реализован",
        "реализована",
        "реализовано",
        "формирует",
    }
    _extension_directives: ClassVar[tuple[str, ...]] = (
        "directive=Вместо",
        "directive=ИзменениеИКонтроль",
        "directive=Перед",
        "directive=После",
    )
    _suffixes = tuple(
        sorted(
            {
                "иями",
                "ями",
                "ами",
                "его",
                "ого",
                "ему",
                "ому",
                "ыми",
                "ими",
                "ий",
                "ый",
                "ая",
                "яя",
                "ое",
                "ее",
                "ые",
                "ие",
                "ам",
                "ям",
                "ах",
                "ях",
                "ов",
                "ев",
                "ом",
                "ем",
                "ой",
                "ей",
                "ую",
                "юю",
                "а",
                "я",
                "ы",
                "и",
                "у",
                "ю",
                "е",
            },
            key=len,
            reverse=True,
        )
    )

    def __init__(self, retrieval: RetrievalService) -> None:
        self.retrieval = retrieval

    def plan(
        self,
        project_name: str,
        question: str,
        *,
        include: tuple[str, ...] = (),
        term_override: str | None = None,
        limit: int = 12,
    ) -> AskPlan:
        question = question.strip()
        if not question:
            raise AskError("Question must not be empty.")
        if term_override:
            explicit_matches = self.retrieval.find(
                project_name,
                term_override,
                include=include,
                limit=max(20, limit),
            )
            if not explicit_matches:
                raise AskError(f"Nothing found for explicit term: {term_override}")
            planned = tuple(
                PlannedMatch(10_000 - index, (term_override,), item)
                for index, item in enumerate(explicit_matches[:limit])
            )
            return AskPlan(question, (term_override,), term_override, planned)

        terms, roots = self._search_terms(question)
        prefer_code = self._prefers_code(question)
        if not terms:
            raise AskError(
                "The question does not contain a searchable 1C term. Use --term to select "
                "a metadata object, module or method explicitly."
            )

        primary_project = self.retrieval.catalog.get_project(project_name)
        ranked: dict[tuple[int, str, int], tuple[int, set[str], EntityRef]] = {}
        for term in terms[:14]:
            variants = (term, term[:1].upper() + term[1:])
            for search_term in dict.fromkeys(variants):
                for item in self.retrieval.find(
                    project_name,
                    search_term,
                    include=include,
                    limit=50,
                ):
                    key = (item.project_id, item.kind, item.id)
                    matched = {
                        root for root in roots if root in normalize(f"{item.name} {item.details}")
                    }
                    if not matched:
                        matched = {self._stem(term)}
                    score = self._score(
                        item,
                        matched,
                        roots,
                        primary_project.id,
                        search_term,
                        prefer_code=prefer_code,
                    )
                    previous = ranked.get(key)
                    if previous is None or score > previous[0]:
                        ranked[key] = (score, matched, item)
                    else:
                        previous[1].update(matched)

        if prefer_code:
            parent_candidates = [
                item
                for _score, _matched_terms, item in sorted(
                    ranked.values(),
                    key=lambda row: (
                        -row[0],
                        0 if row[2].project_id == primary_project.id else 1,
                        row[2].name,
                    ),
                )
                if item.kind != "symbol"
            ][:16]
            for item in self.retrieval.symbols_for_entities(
                parent_candidates,
                limit_per_entity=24,
            ):
                key = (item.project_id, item.kind, item.id)
                matched = {
                    root for root in roots if root in normalize(f"{item.name} {item.details}")
                }
                score = self._score(
                    item,
                    matched,
                    roots,
                    primary_project.id,
                    "",
                    prefer_code=True,
                )
                previous = ranked.get(key)
                if previous is None or score > previous[0]:
                    ranked[key] = (score, matched, item)
                else:
                    previous[1].update(matched)

        planned_matches = sorted(
            (
                PlannedMatch(score, tuple(sorted(matched_terms)), item)
                for score, matched_terms, item in ranked.values()
            ),
            key=lambda planned: (
                -planned.score,
                0 if planned.entity.project_id == primary_project.id else 1,
                planned.entity.name,
            ),
        )
        if not planned_matches:
            raise AskError(
                "No indexed 1C entities matched the question. Use --term with an exact "
                "object, module or method name."
            )
        selected = planned_matches[0].entity.name
        return AskPlan(question, terms, selected, tuple(planned_matches[:limit]))

    def _search_terms(self, question: str) -> tuple[tuple[str, ...], set[str]]:
        explicit = [match.group(0).strip() for match in self._qualified_re.finditer(question)]
        explicit.extend(
            value.strip() for value in self._quoted_re.findall(question) if value.strip()
        )
        words = [word.lower() for word in self._word_re.findall(question)]
        significant = [
            word
            for word in words
            if len(word) >= 3 and word not in self._stopwords and not word.isdigit()
        ]
        roots = {self._stem(word) for word in significant}
        roots = {root for root in roots if len(root) >= 3}

        phrases: list[str] = []
        for size in (4, 3, 2):
            for start in range(0, max(0, len(significant) - size + 1)):
                phrase = " ".join(significant[start : start + size])
                phrases.append(phrase)
        ordered = [
            *explicit,
            *significant,
            *sorted(roots, key=lambda item: (-len(item), item)),
            *phrases,
        ]
        result: list[str] = []
        seen: set[str] = set()
        for item in ordered:
            normalized = normalize(item)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(item)
        return tuple(result), roots

    def _score(
        self,
        item: EntityRef,
        matched: set[str],
        roots: set[str],
        primary_project_id: int,
        searched_term: str,
        *,
        prefer_code: bool,
    ) -> int:
        haystack = normalize(f"{item.name} {item.details}")
        score = sum(8 + min(12, len(root)) for root in matched)
        if roots and roots.issubset({root for root in roots if root in haystack}):
            score += 80
        normalized_search = normalize(searched_term)
        normalized_name = normalize(item.name)
        if normalized_search and normalized_search == normalized_name:
            score += 150
        elif normalized_search and normalized_search in normalized_name:
            score += 35
        if item.project_id == primary_project_id:
            score += 45
        if prefer_code:
            score += {"metadata": 0, "module": 26, "symbol": 52}[item.kind]
            if item.kind == "metadata" and ".Attribute." in item.name:
                score -= 20
            if item.kind == "symbol":
                if any(directive in item.details for directive in self._extension_directives):
                    score += 70
                method_name = normalize(item.name.rsplit(".", 1)[-1])
                if any(root.startswith("печат") for root in roots) and (
                    "печат" in method_name or "сформир" in method_name
                ):
                    score += 35
        else:
            score += {"metadata": 24, "module": 20, "symbol": 16}[item.kind]
        if item.kind == "symbol" and "export=True" in item.details:
            score += 5
        return score

    def _prefers_code(self, question: str) -> bool:
        words = {word.casefold() for word in self._word_re.findall(question)}
        if words & self._code_intent_words:
            return True
        normalized_question = normalize(question)
        return any(
            marker in normalized_question
            for marker in (
                "цепочквызов",
                "точквход",
                "исходнкод",
                "модул",
                "процедур",
                "функц",
            )
        )

    def _stem(self, word: str) -> str:
        normalized = normalize(word)
        for suffix in self._suffixes:
            if len(normalized) - len(suffix) >= 4 and normalized.endswith(suffix):
                return normalized[: -len(suffix)]
        return normalized


class AskService:
    """Prepare source-backed context, call an LLM and persist a reproducible run."""

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or Settings()
        self.retrieval = RetrievalService(session)
        self.planner = QuestionPlanner(self.retrieval)
        self.provider = provider

    def ask(
        self,
        project_name: str,
        question: str,
        *,
        include: tuple[str, ...] = (),
        term: str | None = None,
        model: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        depth: int = 2,
        max_chars: int = 60_000,
        max_units: int = 30,
        max_prompt_chars: int = 180_000,
        max_output_tokens: int = 4_000,
        output_root: Path | None = None,
        dry_run: bool = False,
    ) -> AskResult:
        if max_prompt_chars < 20_000:
            raise AskError("Prompt budget must be at least 20000 characters.")
        selected_model = model or self.settings.openai_model
        selected_reasoning = reasoning_effort or self.settings.openai_reasoning_effort
        run_directory = self._run_directory(output_root, question)
        run_directory.mkdir(parents=True, exist_ok=False)
        context_path = run_directory / "context.json"
        prompt_path = run_directory / "prompt.txt"
        request_path = run_directory / "request.json"
        run_path = run_directory / "run.json"
        answer_path = run_directory / "answer.md"
        selected_term = term

        try:
            plan = self.planner.plan(
                project_name,
                question,
                include=include,
                term_override=term,
            )
            selected_term = plan.selected_term
            context_payload = self.retrieval.build_context(
                project_name,
                plan.selected_term,
                include=include,
                depth=depth,
                max_chars=max_chars,
                max_units=max_units,
            )
            context_payload["ask"] = {
                "question": question,
                "plan": self._plan_dict(plan),
            }
            context_text = json.dumps(
                context_payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            context_path.write_text(context_text, encoding="utf-8")

            prompt_context = self._bounded_prompt_context(context_payload, max_prompt_chars)
            instructions = self._instructions()
            input_text = self._input_text(question, plan, prompt_context)
            prompt_path.write_text(
                f"# INSTRUCTIONS\n{instructions}\n\n# INPUT\n{input_text}\n",
                encoding="utf-8",
            )
            request_payload = {
                "schema": "open1c-analyzer-ask-request-v1",
                "created_at": datetime.now(UTC).isoformat(),
                "project": project_name,
                "include": list(include),
                "question": question,
                "term_override": term,
                "selected_term": plan.selected_term,
                "model": selected_model,
                "reasoning_effort": selected_reasoning,
                "depth": depth,
                "max_chars": max_chars,
                "max_units": max_units,
                "max_prompt_chars": max_prompt_chars,
                "max_output_tokens": max_output_tokens,
                "dry_run": dry_run,
                "plan": self._plan_dict(plan),
                "context_sha256": _digest(context_text),
                "prompt_sha256": _digest(prompt_path.read_text(encoding="utf-8")),
            }
            request_path.write_text(
                json.dumps(request_payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

            if dry_run:
                self._write_run(
                    run_path,
                    status="prepared",
                    model=selected_model,
                    selected_term=plan.selected_term,
                    context_path=context_path,
                    prompt_path=prompt_path,
                )
                return AskResult(
                    run_directory,
                    plan.selected_term,
                    context_path,
                    prompt_path,
                    None,
                    None,
                    selected_model,
                    True,
                )

            provider = self.provider or OpenAIResponsesProvider(self.settings)
            provider_result = provider.answer(
                model=selected_model,
                instructions=instructions,
                input_text=input_text,
                reasoning_effort=selected_reasoning,
                max_output_tokens=max_output_tokens,
            )
            if not provider_result.text:
                raise AskError("The model returned an empty answer.")
            answer_path.write_text(provider_result.text + "\n", encoding="utf-8")
            self._write_run(
                run_path,
                status="completed",
                model=selected_model,
                selected_term=plan.selected_term,
                context_path=context_path,
                prompt_path=prompt_path,
                answer_path=answer_path,
                provider_result=provider_result,
            )
            return AskResult(
                run_directory,
                plan.selected_term,
                context_path,
                prompt_path,
                answer_path,
                provider_result.text,
                selected_model,
                False,
                provider_result.request_id,
                provider_result.response_id,
                provider_result.input_tokens,
                provider_result.output_tokens,
                provider_result.total_tokens,
            )
        except Exception as exc:
            self._write_run(
                run_path,
                status="failed",
                model=selected_model,
                selected_term=selected_term,
                context_path=context_path if context_path.exists() else None,
                prompt_path=prompt_path if prompt_path.exists() else None,
                error=str(exc),
            )
            if isinstance(exc, (AskError, ProjectCatalogError)):
                raise
            raise AskError(f"Ask run failed: {exc}") from exc

    def _bounded_prompt_context(
        self,
        payload: dict[str, Any],
        max_prompt_chars: int,
    ) -> str:
        compact: dict[str, Any] = {
            "schema": payload["schema"],
            "request": payload["request"],
            "projects": payload["projects"],
            "matches": payload["matches"],
            "source_units": payload["source_units"],
            "calls": payload["calls"],
            "impact": payload["impact"],
            "queries": payload["queries"],
            "unresolved_near_context": payload["unresolved_near_context"],
        }
        text = _compact_json(compact)
        if len(text) <= max_prompt_chars:
            return text

        # Preserve source code and exact matches. Trim graph/noise lists progressively.
        for key, minimum in (
            ("unresolved_near_context", 20),
            ("impact", 60),
            ("calls", 80),
            ("queries", 10),
        ):
            rows = compact[key]
            while len(text) > max_prompt_chars and len(rows) > minimum:
                del rows[max(minimum, len(rows) // 2) :]
                text = _compact_json(compact)
        if len(text) <= max_prompt_chars:
            return text

        # Last resort: shrink source bodies proportionally while preserving signatures/locations.
        source_units = compact["source_units"]
        while len(text) > max_prompt_chars and source_units:
            changed = False
            for unit in reversed(source_units):
                source = str(unit.get("source", ""))
                if len(source) <= 800:
                    continue
                unit["source"] = source[: max(800, int(len(source) * 0.8))] + "\n...<truncated>"
                changed = True
                text = _compact_json(compact)
                if len(text) <= max_prompt_chars:
                    break
            if not changed:
                break
        if len(text) > max_prompt_chars:
            raise AskError(
                "The prepared context does not fit the prompt budget. Increase "
                "--max-prompt-chars or reduce --max-chars/--max-units."
            )
        return text

    @staticmethod
    def _instructions() -> str:
        return (
            "Ты эксперт по архитектуре и разработке 1С:Предприятие. Отвечай только по "
            "переданному статическому контексту. Не придумывай отсутствующие объекты, методы, "
            "параметры или поведение платформы. Отделяй подтвержденные факты от выводов. "
            "Каждый существенный вывод о коде сопровождай ссылкой вида "
            "[PROJECT:relative/path.bsl:LINE_START-LINE_END]. Если данных недостаточно, "
            "прямо укажи, чего именно не хватает. Сначала дай прямой ответ, затем кратко "
            "опиши цепочку вызовов и затронутые объекты. Пиши по-русски."
        )

    @staticmethod
    def _input_text(question: str, plan: AskPlan, context: str) -> str:
        candidates = "\n".join(
            " | ".join(
                (
                    f"- {item.entity.project}",
                    item.entity.kind,
                    item.entity.name,
                    f"score={item.score}",
                )
            )
            for item in plan.matches[:8]
        )
        return (
            f"ВОПРОС:\n{question}\n\n"
            f"ВЫБРАННАЯ ТОЧКА ВХОДА:\n{plan.selected_term}\n\n"
            f"КАНДИДАТЫ ПЛАНИРОВЩИКА:\n{candidates}\n\n"
            f"КОНТЕКСТ OPEN1C ANALYZER (JSON):\n{context}"
        )

    @staticmethod
    def _plan_dict(plan: AskPlan) -> dict[str, Any]:
        return {
            "question": plan.question,
            "search_terms": list(plan.search_terms),
            "selected_term": plan.selected_term,
            "matches": [
                {
                    "score": item.score,
                    "matched_terms": list(item.matched_terms),
                    "entity": asdict(item.entity),
                }
                for item in plan.matches
            ],
        }

    def _run_directory(self, output_root: Path | None, question: str) -> Path:
        root = (output_root or self.settings.runs_path).expanduser().resolve()
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        slug = _slug(question)
        return root / f"{timestamp}-{slug}"

    @staticmethod
    def _write_run(
        path: Path,
        *,
        status: str,
        model: str,
        selected_term: str | None,
        context_path: Path | None,
        prompt_path: Path | None,
        answer_path: Path | None = None,
        provider_result: ProviderResult | None = None,
        error: str | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "schema": "open1c-analyzer-ask-run-v1",
            "updated_at": datetime.now(UTC).isoformat(),
            "status": status,
            "model": model,
            "selected_term": selected_term,
            "context_file": context_path.name if context_path else None,
            "prompt_file": prompt_path.name if prompt_path else None,
            "answer_file": answer_path.name if answer_path else None,
            "error": error,
        }
        if provider_result is not None:
            payload["provider"] = {
                "response_id": provider_result.response_id,
                "request_id": provider_result.request_id,
                "input_tokens": provider_result.input_tokens,
                "output_tokens": provider_result.output_tokens,
                "total_tokens": provider_result.total_tokens,
            }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", value.lower())[:7]
    slug = "-".join(words)[:80]
    return slug or "question"
