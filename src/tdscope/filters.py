"""Thread selection shared by all analyses."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .model import ThreadInfo


@dataclass
class ThreadFilter:
    """Selects threads and stack frames.

    ``frame_patterns`` are substrings (or regular expressions with ``regex=True``),
    e.g. ``io.wcm.`` or ``com.mycompany.``. A thread matches when any of its frames
    matches any pattern; with no patterns every thread matches.
    """

    frame_patterns: Sequence[str] = ()
    regex: bool = False
    states: Sequence[str] = ()
    name_pattern: str | None = None
    http_only: bool = False
    _compiled: list[re.Pattern[str]] = field(init=False, repr=False)
    _name_re: re.Pattern[str] | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.regex:
            self._compiled = [re.compile(p) for p in self.frame_patterns]
        else:
            self._compiled = [re.compile(re.escape(p)) for p in self.frame_patterns]
        self._name_re = re.compile(self.name_pattern) if self.name_pattern else None
        self.states = [s.upper() for s in self.states]

    @property
    def has_frame_patterns(self) -> bool:
        return bool(self._compiled)

    def frame_matches(self, frame: str) -> bool:
        return any(p.search(frame) for p in self._compiled)

    def first_matching_frame(self, thread: ThreadInfo) -> str | None:
        """Top-most frame matching the patterns: where "your" code currently executes."""
        return next((f for f in thread.frames if self.frame_matches(f)), None)

    def matches(self, thread: ThreadInfo) -> bool:
        if self.http_only and thread.request is None:
            return False
        if self.states and (thread.state or "") not in self.states:
            return False
        if self._name_re and not self._name_re.search(thread.name):
            return False
        return not (self._compiled and not any(self.frame_matches(f) for f in thread.frames))
