"""The set of log formats LogSherlock can detect.

This enum is the shared vocabulary for "what kind of logs are these?". The
parser produces it during format detection and surfaces its string value on
:class:`~graph_library.models.parser_metrics.ParserMetrics.detected_format`, so downstream
nodes can branch on a stable, spelled-out value instead of a magic string.

Add a member here (and a matching parser in the ``graph_library.parser`` package) to support
another format.
"""

from __future__ import annotations

from enum import Enum


class LogFormat(str, Enum):
    """A log format LogSherlock knows how to read.

    Inherits from ``str`` so the value is JSON-serialisable and can be dropped
    straight into graph state / metrics without conversion.
    """

    JSON = "json"
    TEXT = "text"
