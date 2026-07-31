"""Fault-tolerant structural analysis of 1C:Enterprise BSL modules."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256

from open1c_analyzer.parser.names import MANAGER_KIND, TYPE_KIND, full_metadata_name, normalize
from open1c_analyzer.parser.query import QueryTable, analyze_query

_IDENT_START = re.compile(r"[A-Za-zА-Яа-яЁё_]")
_IDENT_CONT = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]")
_REGION = re.compile(r"^\s*#(?:Область|Region)\s+(?P<name>.+?)\s*$", re.IGNORECASE)
_END_REGION = re.compile(r"^\s*#(?:КонецОбласти|EndRegion)\b", re.IGNORECASE)
_DIRECTIVE = re.compile(r"^\s*&(?P<name>[A-Za-zА-Яа-яЁё0-9_]+)", re.IGNORECASE)
_DECL = {
    "процедура": ("procedure", {"конецпроцедуры", "endprocedure"}),
    "procedure": ("procedure", {"конецпроцедуры", "endprocedure"}),
    "функция": ("function", {"конецфункции", "endfunction"}),
    "function": ("function", {"конецфункции", "endfunction"}),
}
_EXCLUDE_CALLS = {
    "and",
    "do",
    "else",
    "elseif",
    "for",
    "function",
    "if",
    "new",
    "not",
    "or",
    "procedure",
    "return",
    "then",
    "to",
    "try",
    "while",
    "возврат",
    "для",
    "если",
    "и",
    "иначе",
    "иначеесли",
    "или",
    "не",
    "новый",
    "по",
    "пока",
    "попытка",
    "процедура",
    "тогда",
    "функция",
    "цикл",
}


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    line: int
    column: int
    end_line: int


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    by_value: bool
    default: str | None


@dataclass(frozen=True, slots=True)
class ParsedSymbol:
    ordinal: int
    name: str
    kind: str
    is_export: bool
    directive: str | None
    region: str | None
    line_start: int
    line_end: int
    signature: str
    parameters: tuple[Parameter, ...]
    body_hash: str

    def parameters_json(self) -> str:
        return json.dumps(
            [
                {"name": item.name, "by_value": item.by_value, "default": item.default}
                for item in self.parameters
            ],
            ensure_ascii=False,
        )


@dataclass(frozen=True, slots=True)
class ParsedCall:
    caller_ordinal: int | None
    name: str
    qualifier: str | None
    full_name: str
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    caller_ordinal: int | None
    line_start: int
    line_end: int
    kind: str
    text: str
    tables: tuple[QueryTable, ...]


@dataclass(frozen=True, slots=True)
class ParsedReference:
    caller_ordinal: int | None
    target_kind: str
    target_name: str
    target_full_name: str
    relation: str
    line: int
    raw_value: str


@dataclass(frozen=True, slots=True)
class ParsedModule:
    directives: tuple[str, ...]
    symbols: tuple[ParsedSymbol, ...]
    calls: tuple[ParsedCall, ...]
    queries: tuple[ParsedQuery, ...]
    references: tuple[ParsedReference, ...]


class Lexer:
    """Small lexer that ignores comments but preserves multiline strings."""

    def tokenize(self, text: str) -> list[Token]:
        result: list[Token] = []
        index = 0
        line = 1
        column = 1
        while index < len(text):
            char = text[index]
            if char in " \t\f\v":
                index += 1
                column += 1
            elif char in "\r\n":
                if char == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                    index += 1
                result.append(Token("newline", "\n", line, column, line))
                index += 1
                line += 1
                column = 1
            elif char == "/" and index + 1 < len(text) and text[index + 1] == "/":
                while index < len(text) and text[index] not in "\r\n":
                    index += 1
                    column += 1
            elif char == '"':
                start_line, start_column = line, column
                value: list[str] = []
                index += 1
                column += 1
                while index < len(text):
                    current = text[index]
                    if current == '"':
                        if index + 1 < len(text) and text[index + 1] == '"':
                            value.append('"')
                            index += 2
                            column += 2
                        else:
                            index += 1
                            column += 1
                            break
                    elif current in "\r\n":
                        if current == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                            index += 1
                        value.append("\n")
                        index += 1
                        line += 1
                        column = 1
                    else:
                        value.append(current)
                        index += 1
                        column += 1
                result.append(Token("string", "".join(value), start_line, start_column, line))
            elif _IDENT_START.fullmatch(char):
                start, start_column = index, column
                while index < len(text) and _IDENT_CONT.fullmatch(text[index]):
                    index += 1
                    column += 1
                result.append(Token("identifier", text[start:index], line, start_column, line))
            else:
                result.append(Token("symbol", char, line, column, line))
                index += 1
                column += 1
        return result


class BslAnalyzer:
    """Extract methods, calls, queries and metadata references."""

    def parse(self, text: str) -> ParsedModule:
        tokens = Lexer().tokenize(text)
        lines = text.splitlines()
        regions = self._regions(lines)
        symbols = self._symbols(tokens, lines, regions)
        directives = tuple(
            match.group("name") for line in lines if (match := _DIRECTIVE.match(line))
        )
        return ParsedModule(
            directives,
            tuple(symbols),
            tuple(self._calls(tokens, symbols)),
            tuple(self._queries(tokens, symbols)),
            tuple(self._references(tokens, symbols)),
        )

    @staticmethod
    def _regions(lines: list[str]) -> dict[int, str | None]:
        stack: list[str] = []
        result: dict[int, str | None] = {}
        for number, line in enumerate(lines, 1):
            if match := _REGION.match(line):
                stack.append(match.group("name").strip())
            elif _END_REGION.match(line) and stack:
                stack.pop()
            result[number] = stack[-1] if stack else None
        return result

    def _symbols(
        self, tokens: list[Token], lines: list[str], regions: dict[int, str | None]
    ) -> list[ParsedSymbol]:
        result: list[ParsedSymbol] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            declaration = _DECL.get(token.value.casefold()) if token.kind == "identifier" else None
            if declaration is None:
                index += 1
                continue
            kind, end_words = declaration
            name_index = self._next_non_newline(tokens, index + 1)
            if name_index is None or tokens[name_index].kind != "identifier":
                index += 1
                continue
            open_index = self._next_value(tokens, name_index + 1, "(")
            if open_index is None:
                index += 1
                continue
            close_index = self._match_parenthesis(tokens, open_index) or open_index
            end_index = next(
                (
                    i
                    for i in range(close_index + 1, len(tokens))
                    if tokens[i].kind == "identifier" and tokens[i].value.casefold() in end_words
                ),
                None,
            )
            line_end = (
                tokens[end_index].line if end_index is not None else max(len(lines), token.line)
            )
            tail_end = next(
                (i for i in range(close_index + 1, len(tokens)) if tokens[i].kind == "newline"),
                len(tokens),
            )
            is_export = any(
                item.value.casefold() in {"экспорт", "export"}
                for item in tokens[close_index + 1 : tail_end]
            )
            signature = "\n".join(lines[token.line - 1 : tokens[close_index].end_line]).strip()
            body = "\n".join(lines[token.line - 1 : line_end])
            result.append(
                ParsedSymbol(
                    len(result),
                    tokens[name_index].value,
                    kind,
                    is_export,
                    self._directive_before(lines, token.line),
                    regions.get(token.line),
                    token.line,
                    line_end,
                    signature,
                    self._parameters(tokens[open_index + 1 : close_index]),
                    sha256(body.encode("utf-8")).hexdigest(),
                )
            )
            index = (end_index + 1) if end_index is not None else len(tokens)
        return result

    @staticmethod
    def _next_non_newline(tokens: list[Token], start: int) -> int | None:
        return next((i for i in range(start, len(tokens)) if tokens[i].kind != "newline"), None)

    @staticmethod
    def _next_value(tokens: list[Token], start: int, value: str) -> int | None:
        return next(
            (
                i
                for i in range(start, len(tokens))
                if tokens[i].kind != "newline" and tokens[i].value == value
            ),
            None,
        )

    @staticmethod
    def _match_parenthesis(tokens: list[Token], start: int) -> int | None:
        depth = 0
        for index in range(start, len(tokens)):
            depth += tokens[index].value == "("
            depth -= tokens[index].value == ")"
            if depth == 0:
                return index
        return None

    @staticmethod
    def _directive_before(lines: list[str], line_number: int) -> str | None:
        index = line_number - 2
        while index >= 0 and not lines[index].strip():
            index -= 1
        match = _DIRECTIVE.match(lines[index]) if index >= 0 else None
        return match.group("name") if match else None

    @staticmethod
    def _parameters(tokens: list[Token]) -> tuple[Parameter, ...]:
        chunks: list[list[Token]] = [[]]
        depth = 0
        for token in tokens:
            if token.value in "([{":
                depth += 1
            elif token.value in ")]}":
                depth = max(0, depth - 1)
            if token.value == "," and depth == 0:
                chunks.append([])
            else:
                chunks[-1].append(token)
        result: list[Parameter] = []
        for chunk in chunks:
            identifiers = [item for item in chunk if item.kind == "identifier"]
            if not identifiers:
                continue
            by_value = identifiers[0].value.casefold() in {"знач", "val"}
            name = (
                identifiers[1].value if by_value and len(identifiers) > 1 else identifiers[0].value
            )
            default = None
            if any(item.value == "=" for item in chunk):
                pos = next(i for i, item in enumerate(chunk) if item.value == "=")
                default = " ".join(item.value for item in chunk[pos + 1 :]).strip() or None
            result.append(Parameter(name, by_value, default))
        return tuple(result)

    def _calls(self, tokens: list[Token], symbols: list[ParsedSymbol]) -> list[ParsedCall]:
        result: list[ParsedCall] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.kind != "identifier":
                index += 1
                continue
            parts = [token.value]
            cursor = index
            while (
                cursor + 2 < len(tokens)
                and tokens[cursor + 1].value == "."
                and tokens[cursor + 2].kind == "identifier"
            ):
                parts.append(tokens[cursor + 2].value)
                cursor += 2
            if cursor + 1 >= len(tokens) or tokens[cursor + 1].value != "(":
                index += 1
                continue
            previous = index - 1
            while previous >= 0 and tokens[previous].kind == "newline":
                previous -= 1
            declaration_name = previous >= 0 and tokens[previous].value.casefold() in _DECL
            if parts[0].casefold() not in _EXCLUDE_CALLS and not declaration_name:
                caller = self._symbol_at(symbols, token.line)
                result.append(
                    ParsedCall(
                        caller.ordinal if caller else None,
                        parts[-1],
                        ".".join(parts[:-1]) or None,
                        ".".join(parts),
                        token.line,
                        token.column,
                    )
                )
            index = cursor + 1
        return result

    @staticmethod
    def _queries(tokens: list[Token], symbols: list[ParsedSymbol]) -> list[ParsedQuery]:
        result: list[ParsedQuery] = []
        for token in tokens:
            if token.kind == "string" and (query := analyze_query(token.value)):
                caller = BslAnalyzer._symbol_at(symbols, token.line)
                result.append(
                    ParsedQuery(
                        caller.ordinal if caller else None,
                        token.line,
                        token.end_line,
                        query.kind,
                        query.text,
                        query.tables,
                    )
                )
        return result

    @staticmethod
    def _references(tokens: list[Token], symbols: list[ParsedSymbol]) -> list[ParsedReference]:
        type_map = {normalize(key): value for key, value in TYPE_KIND.items()}
        result: list[ParsedReference] = []
        seen: set[tuple[int, str, str]] = set()
        for index in range(len(tokens) - 2):
            first, dot, second = tokens[index : index + 3]
            if first.kind != "identifier" or dot.value != "." or second.kind != "identifier":
                continue
            prefix = normalize(first.value)
            kind = MANAGER_KIND.get(prefix)
            relation = "manager_access"
            if kind is None:
                kind = type_map.get(prefix)
                relation = "type_reference"
            if kind is None:
                continue
            full_name = full_metadata_name(kind, second.value)
            identity = (first.line, normalize(full_name), relation)
            if identity in seen:
                continue
            seen.add(identity)
            caller = BslAnalyzer._symbol_at(symbols, first.line)
            result.append(
                ParsedReference(
                    caller.ordinal if caller else None,
                    kind,
                    second.value,
                    full_name,
                    relation,
                    first.line,
                    f"{first.value}.{second.value}",
                )
            )
        return result

    @staticmethod
    def _symbol_at(symbols: list[ParsedSymbol], line: int) -> ParsedSymbol | None:
        return next((item for item in symbols if item.line_start <= line <= item.line_end), None)
