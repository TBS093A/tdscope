"""Locating and reading thread dump files."""

from __future__ import annotations

import fnmatch
import gzip
import io
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from .model import ThreadDump
from .parser import parse_lines

DEFAULT_PATTERNS = ("*.dump", "*.tdump", "*.txt", "*.log", "*.out", "*.jstack", "*.gz")


def _open_text(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def discover(paths: Sequence[str], patterns: Sequence[str] = DEFAULT_PATTERNS) -> list[Path]:
    """Expand directories (recursively) into files matching ``patterns``.

    Files given explicitly are always included, whatever their name.
    """
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(files):
                    if any(fnmatch.fnmatch(name, pat) for pat in patterns):
                        found.append(Path(root) / name)
        elif path.is_file():
            found.append(path)
        else:
            raise FileNotFoundError(f"no such file or directory: {raw}")
    return found


def load(
    paths: Sequence[str], patterns: Sequence[str] = DEFAULT_PATTERNS, warn: TextIO | None = None
) -> list[ThreadDump]:
    """Load and parse all dumps, ordered chronologically (dumps without timestamp last)."""
    dumps: list[ThreadDump] = []
    if list(paths) == ["-"]:
        dumps.extend(parse_lines(sys.stdin, source="<stdin>"))
    else:
        for path in discover(paths, patterns):
            with _open_text(path) as fh:
                found = list(parse_lines(fh, source=str(path)))
            if not found:
                print(f"tdscope: no thread dump found in {path}, skipping", file=warn or sys.stderr)
            dumps.extend(found)
    dumps.sort(key=lambda d: (d.timestamp is None, d.timestamp or 0, d.source, d.index))
    return dumps
