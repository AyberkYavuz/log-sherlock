"""LogSherlock deterministic parser package.

Public surface:

    * :func:`parser_node` — the graph node entry point.
    * :class:`ParsedLogEntry`, :class:`LogFormat` — the normalized data model.
    * Parser classes and the factory, for reuse and testing.
"""

from __future__ import annotations

from .base_parser import BaseParser
from .json_parser import JSONLinesParser
from .models import LogFormat, ParsedLogEntry
from .parser_factory import PARSER_REGISTRY, select_parser
from .parser_node import parser_node
from .text_parser import PlainTextParser

__all__ = [
    "parser_node",
    "ParsedLogEntry",
    "LogFormat",
    "BaseParser",
    "JSONLinesParser",
    "PlainTextParser",
    "PARSER_REGISTRY",
    "select_parser",
]
