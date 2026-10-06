"""Tabular views of analysis results (used by the TUI, independent of any UI toolkit).

Every analysis result becomes a :class:`Table`: column headers, one row per result item
and, for each row, a detail text (the regular text report of that single item, with
its example stack).
"""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import analysis as a
from . import report
from .timeline import format_duration


@dataclass
class Table:
    columns: list[str]
    rows: list[list[str]]
    details: list[str]


def _render(render: Callable[..., None], items: Any) -> str:
    buf = io.StringIO()
    render(items, buf, show_stack=True)
    return buf.getvalue().rstrip().removesuffix(report.RULE).rstrip()


def _s(value: float | None) -> str:
    return "-" if value is None else format_duration(value)


def _top(counts: dict[str, int]) -> str:
    return next(iter(counts), "-")


def _summary(result: list[a.DumpSummary]) -> Table:
    states = ("RUNNABLE", "BLOCKED", "WAITING", "TIMED_WAITING")
    rows = [
        [
            s.timestamp.strftime("%Y-%m-%d %H:%M:%S") if s.timestamp else "-",
            s.label.rsplit(" (", 1)[0],
            str(s.threads),
            str(s.selected),
            *(str(s.states.get(st, 0)) for st in states),
            str(s.http_requests),
            str(len(s.deadlocks)) if s.deadlocks else "",
        ]
        for s in result
    ]
    return Table(
        ["time", "dump", "threads", "selected", *states, "http", "deadlocks"],
        rows,
        [_render(report.render_summary, [s]) for s in result],
    )


def _requests(result: list[a.RequestGroup]) -> Table:
    rows = [
        [
            g.key,
            str(g.snapshots),
            str(g.dumps_seen),
            str(g.distinct_requests),
            _s(g.request_age_s.avg if g.request_age_s else None),
            _s(g.request_age_s.max if g.request_age_s else None),
            _s(g.observed_span_s),
            _top(g.states),
            _top(g.top_frames),
        ]
        for g in result
    ]
    columns = ["request", "count", "dumps", "distinct", "age avg", "age max", "span", "state", "top frame"]
    return Table(columns, rows, [_render(report.render_requests, [g]) for g in result])


def _frames(result: list[a.FrameGroup]) -> Table:
    rows = [[str(g.snapshots), str(g.distinct_threads), g.frame] for g in result]
    return Table(["count", "threads", "frame"], rows, [_render(report.render_frames, [g]) for g in result])


def _hotspots(result: list[a.StackGroup]) -> Table:
    rows = [
        [str(g.snapshots), str(g.distinct_threads), _top(g.states), g.signature[0], ", ".join(g.thread_names[:3])]
        for g in result
    ]
    columns = ["count", "threads", "state", "top frame", "thread names"]
    return Table(columns, rows, [_render(report.render_hotspots, [g]) for g in result])


def _stuck(result: list[a.StuckThread]) -> Table:
    rows = [[s.name, s.state or "-", str(s.consecutive_dumps), _s(s.span_s), s.request or ""] for s in result]
    columns = ["thread", "state", "dumps", "span", "request"]
    return Table(columns, rows, [_render(report.render_stuck, [s]) for s in result])


def _locks(result: a.LockReport) -> Table:
    rows: list[list[str]] = []
    details: list[str] = []

    def split(label: str) -> tuple[str, str]:  # "path (2026-05-12 10:13:00)" -> (time, path)
        path, _, when = label.rpartition(" (")
        return (when.rstrip(")"), path) if path else ("-", label)

    for label, names in result.deadlocks:
        when, dump = split(label)
        rows.append([when, str(len(names)), "DEADLOCK", "", " <-> ".join(names), dump])
        details.append(_render(report.render_locks, a.LockReport(deadlocks=[(label, names)])))
    for c in result.contentions:
        when, dump = split(c.dump)
        rows.append([when, str(len(c.waiters)), c.class_name or "?", c.owner_state or "", c.owner or "?", dump])
        details.append(_render(report.render_locks, a.LockReport(contentions=[c])).removeprefix("No lock contention"))
    return Table(["time", "waiters", "lock", "owner state", "owner", "dump"], rows, details)


def _cpu(result: list[a.CpuInterval]) -> Table:
    rows: list[list[str]] = []
    details: list[str] = []
    for interval in result:
        for c in interval.top:
            pct = f"{c.wall_pct:.1f}%" if c.wall_pct is not None else "-"
            rows.append([interval.end, f"{c.cpu_ms:.0f}", pct, c.state or "-", c.name])
            details.append(
                _render(report.render_cpu, [a.CpuInterval(interval.start, interval.end, interval.interval_s, [c])])
            )
    return Table(["interval end", "cpu ms", "% core", "state", "thread"], rows, details)


_VIEWS: dict[str, Callable[[Any], Table]] = {
    "summary": _summary,
    "requests": _requests,
    "frames": _frames,
    "hotspots": _hotspots,
    "stuck": _stuck,
    "locks": _locks,
    "cpu": _cpu,
}


def to_table(command: str, result: Any, root: str | None = None) -> Table:
    """Table for an analysis result; ``root`` is stripped from file paths for brevity."""
    table = _VIEWS[command](result)
    if root:
        table.rows = [[strip_root(cell, root) for cell in row] for row in table.rows]
        table.details = [strip_root(d, root) for d in table.details]
    return table


def strip_root(text: str, root: str) -> str:
    """Remove the opened folder from file paths inside ``text`` (both separators on Windows)."""
    for sep in {"/", os.sep}:
        text = text.replace(root.rstrip("/" + os.sep) + sep, "")
    return text
