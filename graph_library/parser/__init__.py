"""LogSherlock deterministic parser feature package.

Contains only business logic — the shared data models it produces
(:class:`~graph_library.models.ParsedLogEntry`, :class:`~graph_library.models.ParserMetrics`,
:class:`~graph_library.models.LogFormat`) live in the shared ``graph_library.models`` package and are
imported from there.

Public surface:

    * :func:`parser_node` — the graph node entry point.
    * Parser classes, the factory and :func:`detect` — for reuse and testing.
"""

from __future__ import annotations

from .base_parser import BaseParser
from .json_parser import JSONLinesParser
from .parser_factory import PARSER_REGISTRY, Detection, detect, select_parser
from .parser_node import parser_node
from .text_parser import PlainTextParser
from .timestamps import parse_timestamp

__all__ = [
    "parser_node",
    "BaseParser",
    "JSONLinesParser",
    "PlainTextParser",
    "PARSER_REGISTRY",
    "Detection",
    "detect",
    "select_parser",
    "parse_timestamp",
]
