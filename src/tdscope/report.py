"""Rendering analysis results as human-readable text or JSON."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, TextIO

from . import analysis as a

RULE = "-" * 78


def to_json(result: Any, out: TextIO) -> None:
    def default(obj: Any) -> Any:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"not JSON serializable: {type(obj).__name__}")

    payload = [dataclasses.asdict(r) for r in result] if isinstance(result, list) else dataclasses.asdict(result)
    json.dump(payload, out, indent=2, default=default)
    out.write("\n")


def _fmt_s(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}s"


def _fmt_stats(stats: a.Stats | None, unit: str = "s") -> str:
    if stats is None:
        return "-"
    return f"avg {stats.avg:.1f}{unit} (min {stats.min:.1f}{unit}, max {stats.max:.1f}{unit})"


def _fmt_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in counts.items()) or "-"


def _indent(text: str, prefix: str = "    ", max_lines: int | None = None) -> str:
    lines = text.splitlines()
    if max_lines is not None and len(lines) > max_lines:
        lines = [*lines[:max_lines], f"\t... {len(lines) - max_lines} more lines"]
    return "\n".join(prefix + line for line in lines)


def _stack(out: TextIO, stack: str, show: bool, max_lines: int | None) -> None:
    if show and stack:
        out.write(_indent(stack, max_lines=max_lines) + "\n")


def render_summary(result: Sequence[a.DumpSummary], out: TextIO, **_: Any) -> None:
    for s in result:
        out.write(f"{s.label}\n")
        if s.vm_info:
            out.write(f"  VM:            {s.vm_info}\n")
        out.write(f"  threads:       {s.threads} (selected {s.selected})\n")
        out.write(f"  states:        {_fmt_counts(s.states)}\n")
        out.write(f"  HTTP requests: {s.http_requests}\n")
        for names in s.deadlocks:
            out.write(f"  DEADLOCK:      {' <-> '.join(names)}\n")
        out.write(RULE + "\n")


def render_requests(
    result: Sequence[a.RequestGroup], out: TextIO, show_stack: bool = False, max_lines: int | None = None, **_: Any
) -> None:
    if not result:
        out.write("No HTTP request threads found.\n")
        return
    for g in result:
        out.write(f"{g.key}\n")
        out.write(f"  seen {g.snapshots}x in {g.dumps_seen} dump(s), {g.distinct_requests} distinct request(s)\n")
        age = _fmt_stats(g.request_age_s)
        if g.request_age_invalid:
            age += f"  [{g.request_age_invalid} snapshot(s) start after the dump time - set --tz]"
        out.write(f"  request age:   {age}\n")
        out.write(f"  observed span: {_fmt_s(g.observed_span_s)}\n")
        out.write(f"  thread age:    {_fmt_stats(g.thread_age_s)}   (elapsed=, age of the pooled thread)\n")
        out.write(f"  thread cpu:    {_fmt_stats(g.cpu_ms, 'ms')}\n")
        out.write(f"  states:        {_fmt_counts(g.states)}\n")
        out.write("  frames:\n")
        for frame, count in g.top_frames.items():
            out.write(f"    {count:>5}x  {frame}\n")
        if show_stack:
            out.write(f"  example: {g.example_thread}\n")
            _stack(out, g.example_stack, True, max_lines)
        out.write(RULE + "\n")


def render_frames(
    result: Sequence[a.FrameGroup], out: TextIO, show_stack: bool = False, max_lines: int | None = None, **_: Any
) -> None:
    if not result:
        out.write("No matching frames found.\n")
        return
    if not show_stack:
        out.write(f"{'COUNT':>7} {'THREADS':>7}  FRAME\n")
        for g in result:
            out.write(f"{g.snapshots:>7} {g.distinct_threads:>7}  {g.frame}\n")
        return
    for g in result:
        out.write(f"{g.frame}\n")
        out.write(f"  seen {g.snapshots}x in {g.distinct_threads} thread(s)\n")
        out.write(f"  example: {g.example_thread}\n")
        _stack(out, g.example_stack, True, max_lines)
        out.write(RULE + "\n")


def render_hotspots(
    result: Sequence[a.StackGroup], out: TextIO, show_stack: bool = False, max_lines: int | None = None, **_: Any
) -> None:
    if not result:
        out.write("No threads with a stack found.\n")
        return
    for g in result:
        out.write(f"{g.snapshots}x ({g.distinct_threads} thread(s)), states: {_fmt_counts(g.states)}\n")
        out.write(f"  threads: {', '.join(g.thread_names)}\n")
        if show_stack:
            _stack(out, g.example_stack, True, max_lines)
        else:
            out.write(_indent("\n".join(f"at {f}" for f in g.signature)) + "\n")
        out.write(RULE + "\n")


def render_stuck(
    result: Sequence[a.StuckThread], out: TextIO, show_stack: bool = False, max_lines: int | None = None, **_: Any
) -> None:
    if not result:
        out.write("No stuck threads found.\n")
        return
    for s in result:
        out.write(f'"{s.name}" {s.state or ""}\n')
        out.write(f"  same stack in {s.consecutive_dumps} consecutive dumps, span {_fmt_s(s.span_s)}\n")
        out.write(f"  from {s.first_seen}\n  to   {s.last_seen}\n")
        if s.request:
            out.write(f"  request: {s.request}\n")
        _stack(out, s.example_stack, True, max_lines if show_stack else (max_lines or 15))
        out.write(RULE + "\n")


def render_locks(result: a.LockReport, out: TextIO, **_: Any) -> None:
    for label, names in result.deadlocks:
        out.write(f"DEADLOCK in {label}: {' <-> '.join(names)}\n")
    if result.deadlocks:
        out.write(RULE + "\n")
    if not result.contentions:
        out.write("No lock contention found.\n")
        return
    for c in result.contentions:
        out.write(f"<{c.address}> ({c.class_name or '?'}) in {c.dump}\n")
        if c.owner:
            out.write(f'  held by: "{c.owner}" {c.owner_state or ""}\n')
            if c.owner_frame:
                out.write(f"           at {c.owner_frame}\n")
        else:
            out.write("  held by: (owner not found in dump)\n")
        out.write(f"  {len(c.waiters)} waiter(s):\n")
        for name in c.waiters:
            out.write(f'    "{name}"\n')
        out.write(RULE + "\n")


def render_cpu(result: Sequence[a.CpuInterval], out: TextIO, **_: Any) -> None:
    if not result:
        out.write("CPU analysis needs at least two dumps.\n")
        return
    for interval in result:
        out.write(f"{interval.start}\n  -> {interval.end}  (interval {_fmt_s(interval.interval_s)})\n")
        if not interval.top:
            out.write("  no CPU data (dumps from JDK < 11?)\n")
        for c in interval.top:
            pct = f"{c.wall_pct:>6.1f}%" if c.wall_pct is not None else "     -"
            out.write(f'  {c.cpu_ms:>10.1f}ms {pct}  {c.state or "":<13} "{c.name}"\n')
            if c.top_frame:
                out.write(f"  {'':>27}at {c.top_frame}\n")
        out.write(RULE + "\n")
