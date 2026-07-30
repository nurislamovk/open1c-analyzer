"""Extract referenced tables from 1C query text."""

import re
from dataclasses import dataclass

from open1c_analyzer.parser.names import normalize

_MARKER = re.compile(
    r"\b(ВЫБРАТЬ|SELECT|УДАЛИТЬ|DELETE|ОБНОВИТЬ|UPDATE|ВСТАВИТЬ|INSERT)\b", re.IGNORECASE
)
_TABLE = re.compile(
    r"(?:\bИЗ\b|\bFROM\b|\bСОЕДИНЕНИЕ\b|\bJOIN\b|\bUPDATE\b|\bИЗМЕНИТЬ\b|"
    r"\bINSERT\s+INTO\b|\bВСТАВИТЬ\s+В\b|\bDELETE\s+FROM\b|\bУДАЛИТЬ\s+ИЗ\b)\s+"
    r"(?P<name>[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*(?:\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*){0,4})",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class QueryTable:
    name: str
    normalized: str
    operation: str


@dataclass(frozen=True, slots=True)
class QueryInfo:
    kind: str
    text: str
    tables: tuple[QueryTable, ...]


def analyze_query(value: str) -> QueryInfo | None:
    lines = []
    for raw in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.lstrip()
        lines.append((line[1:] if line.startswith("|") else line).rstrip())
    text = "\n".join(lines).strip()
    marker = _MARKER.search(text)
    if marker is None:
        return None
    key = marker.group(1).casefold()
    kind = (
        "select"
        if key in {"выбрать", "select"}
        else "update"
        if key in {"обновить", "update"}
        else "delete"
        if key in {"удалить", "delete"}
        else "insert"
    )
    tables: list[QueryTable] = []
    seen: set[tuple[str, str]] = set()
    for match in _TABLE.finditer(text):
        name = match.group("name")
        prefix = match.group(0)[: match.group(0).find(name)].casefold()
        operation = (
            "write"
            if any(
                word in prefix
                for word in ("update", "изменить", "insert", "вставить", "delete", "удалить")
            )
            else "read"
        )
        identity = (normalize(name), operation)
        if identity not in seen:
            seen.add(identity)
            tables.append(QueryTable(name, identity[0], operation))
    return QueryInfo(kind, text, tuple(tables))
