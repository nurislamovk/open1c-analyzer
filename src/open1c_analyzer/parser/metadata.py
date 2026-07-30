"""Extract metadata objects, children and type references from 1C XML."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import PurePosixPath

from open1c_analyzer.parser.names import (
    CHILD_KIND_MAP,
    DIRECTORY_KIND_MAP,
    TYPE_KIND,
    XML_KIND_MAP,
    full_metadata_name,
    local_name,
)

_TYPE_RE = re.compile(
    r"(?:(?:cfg|v8):)?(?P<prefix>"
    + "|".join(re.escape(key) for key in TYPE_KIND)
    + r")\.(?P<name>[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)",
    re.IGNORECASE,
)
_PROFILE_KEYS = {"CompatibilityMode", "DefaultRunMode", "DataLockControlMode"}
_PROPERTY_KEYS = _PROFILE_KEYS | {
    "Name",
    "Comment",
    "ClientManagedApplication",
    "ClientOrdinaryApplication",
    "Server",
    "ServerCall",
    "Global",
    "Privileged",
    "ReturnValuesReuse",
    "ExternalConnection",
}


@dataclass(frozen=True, slots=True)
class MetadataItem:
    kind: str
    name: str
    full_name: str
    parent_full_name: str | None
    uuid: str | None
    synonym: str | None
    properties_json: str | None


@dataclass(frozen=True, slots=True)
class MetadataRef:
    source_full_name: str
    target_kind: str
    target_name: str
    target_full_name: str
    raw_value: str


@dataclass(frozen=True, slots=True)
class MetadataDocument:
    objects: tuple[MetadataItem, ...]
    references: tuple[MetadataRef, ...]
    profile: dict[str, str]


class MetadataParser:
    def parse(self, text: str, relative_path: str) -> MetadataDocument:
        root = ET.fromstring(text)
        main = self._main(root)
        fallback_kind, fallback_name = self._path_identity(relative_path)
        kind = XML_KIND_MAP.get(local_name(main.tag), fallback_kind)
        name = self._name(main) or fallback_name or local_name(main.tag)
        full_name = full_metadata_name(kind, name)
        objects = [self._item(main, kind, name, full_name, None)]
        references = self._references(main, full_name)
        seen = {full_name.casefold()}
        for element in main.iter():
            if element is main:
                continue
            child_kind = CHILD_KIND_MAP.get(local_name(element.tag))
            child_name = self._name(element) if child_kind else None
            if child_kind is None or not child_name:
                continue
            child_full = f"{full_name}.{local_name(element.tag)}.{child_name}"
            if child_full.casefold() in seen:
                continue
            seen.add(child_full.casefold())
            objects.append(self._item(element, child_kind, child_name, child_full, full_name))
            references.extend(self._references(element, child_full))
        return MetadataDocument(tuple(objects), tuple(references), self._profile(main))

    @staticmethod
    def _main(root: ET.Element) -> ET.Element:
        if local_name(root.tag) in XML_KIND_MAP:
            return root
        return next((child for child in root if local_name(child.tag) in XML_KIND_MAP), root)

    @staticmethod
    def _path_identity(relative_path: str) -> tuple[str, str]:
        path = PurePosixPath(relative_path.replace("\\", "/"))
        parts = list(path.parts)
        for index, part in enumerate(parts):
            if part in DIRECTORY_KIND_MAP and index + 1 < len(parts):
                return DIRECTORY_KIND_MAP[part], PurePosixPath(parts[index + 1]).stem
        if path.name.casefold() == "configuration.xml":
            return "configuration", "Configuration"
        return "metadata", path.stem

    @staticmethod
    def _name(element: ET.Element) -> str | None:
        properties = next(
            (child for child in element if local_name(child.tag) == "Properties"), None
        )
        root = properties if properties is not None else element
        return next(
            (
                child.text.strip()
                for child in root.iter()
                if local_name(child.tag) == "Name" and child.text and child.text.strip()
            ),
            None,
        )

    def _item(
        self, element: ET.Element, kind: str, name: str, full_name: str, parent: str | None
    ) -> MetadataItem:
        properties = self._properties(element)
        return MetadataItem(
            kind,
            name,
            full_name,
            parent,
            element.attrib.get("uuid") or element.attrib.get("UUID"),
            self._synonym(element),
            json.dumps(properties, ensure_ascii=False, sort_keys=True) if properties else None,
        )

    @staticmethod
    def _synonym(element: ET.Element) -> str | None:
        for candidate in element.iter():
            if local_name(candidate.tag) != "Synonym":
                continue
            content = next(
                (
                    child.text.strip()
                    for child in candidate.iter()
                    if local_name(child.tag).casefold() == "content"
                    and child.text
                    and child.text.strip()
                ),
                None,
            )
            if content:
                return content
        return None

    @staticmethod
    def _properties(element: ET.Element) -> dict[str, str]:
        properties = next(
            (child for child in element if local_name(child.tag) == "Properties"), None
        )
        if properties is None:
            return {}
        result: dict[str, str] = {}
        for child in properties:
            key = local_name(child.tag)
            if key not in _PROPERTY_KEYS:
                continue
            value = " ".join(text.strip() for text in child.itertext() if text.strip())
            if value:
                result[key] = value[:4096]
        return result

    @staticmethod
    def _references(element: ET.Element, source: str) -> list[MetadataRef]:
        refs: list[MetadataRef] = []
        seen: set[tuple[str, str]] = set()
        for value in element.itertext():
            for match in _TYPE_RE.finditer(value or ""):
                prefix = match.group("prefix")
                kind = next(
                    mapped
                    for raw, mapped in TYPE_KIND.items()
                    if raw.casefold() == prefix.casefold()
                )
                name = match.group("name")
                key = (kind, name.casefold())
                if key not in seen:
                    seen.add(key)
                    refs.append(
                        MetadataRef(
                            source, kind, name, full_metadata_name(kind, name), match.group(0)
                        )
                    )
        return refs

    @staticmethod
    def _profile(element: ET.Element) -> dict[str, str]:
        result: dict[str, str] = {}
        for child in element.iter():
            key = local_name(child.tag)
            if key in _PROFILE_KEYS:
                value = " ".join(text.strip() for text in child.itertext() if text.strip())
                if value:
                    result[key] = value
        return result
