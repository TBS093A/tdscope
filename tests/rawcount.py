"""Independent, deliberately naive counting of thread dump elements in raw text.

This is the method used to validate the parser against real production dumps: whatever
the parser builds must add up to what plain line matching finds in the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LOCK_LINE = re.compile(r"^\s+- (locked|waiting|parking|eliminated)")


@dataclass(frozen=True)
class RawCounts:
    dumps: int
    threads: int
    frames: int
    states: int
    lock_lines: int


def count(text: str) -> RawCounts:
    dumps = threads = frames = states = locks = 0
    in_deadlock_report = False  # stacks repeated in a JVM deadlock report are not threads
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Found one Java-level deadlock"):
            in_deadlock_report = True
        elif re.match(r"Found \d+ deadlocks?\.", stripped):
            in_deadlock_report = False
        if in_deadlock_report:
            continue
        if line.startswith("Full thread dump"):
            dumps += 1
        elif line.startswith('"'):
            threads += 1
        elif stripped.startswith("at "):
            frames += 1
        elif stripped.startswith("java.lang.Thread.State:"):
            states += 1
        elif _LOCK_LINE.match(line):
            locks += 1
    return RawCounts(dumps, threads, frames, states, locks)
