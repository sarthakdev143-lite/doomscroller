"""CSV export that is safe to open in Excel / Sheets / Numbers.

Formula injection: a cell starting with = + - @ (or a tab / carriage return, which some spreadsheet
apps strip before evaluating) is executed as a formula when the file is opened. Waitlist rows hold
visitor-supplied text (email local parts, channel URLs, roles), so every such cell is prefixed with
a single quote, which spreadsheets show as text. Control characters are dropped.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Iterable

_DANGEROUS = ("=", "+", "-", "@", "\t", "\r", "|", "%")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def safe_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)  # numbers are data, even negative ones
    if isinstance(value, datetime):
        return value.isoformat()
    s = _CONTROL.sub("", str(value))
    if s.startswith(_DANGEROUS) or s.lstrip().startswith(("=", "+", "@")):
        s = "'" + s
    return s


def to_csv(header: list[str], rows: Iterable[Iterable[object]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow([safe_cell(h) for h in header])
    for row in rows:
        w.writerow([safe_cell(c) for c in row])
    # BOM so Excel opens UTF-8 (Devanagari names, emoji) correctly.
    return "﻿" + buf.getvalue()
