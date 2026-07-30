"""Parsers for exported 1C sources."""

from open1c_analyzer.parser.bsl import BslAnalyzer, ParsedModule
from open1c_analyzer.parser.io import read_text
from open1c_analyzer.parser.metadata import MetadataDocument, MetadataParser
from open1c_analyzer.parser.pathing import identify_module

__all__ = [
    "BslAnalyzer",
    "MetadataDocument",
    "MetadataParser",
    "ParsedModule",
    "identify_module",
    "read_text",
]
