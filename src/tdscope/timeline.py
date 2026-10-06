"""Thread timeline chart data: threads over time, colored by group.

Two modes, both with dump time on the X axis:

* ``count``: number of threads of each group in every dump (one line per group);
* ``duration``: one point per thread per dump, Y = how long it has been running
  (request age when the request start is known, otherwise the thread age ``elapsed=``).

The module is UI-agnostic: the TUI draws a :class:`Chart` in the terminal and
:func:`to_html` renders it as a self-contained HTML page with hover tooltips.
"""

from __future__ import annotations

import html
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from .filters import ThreadFilter
from .model import ThreadDump, ThreadInfo

MODES = ("count", "duration")
GROUPINGS = ("pool", "family", "code", "state", "request")
# Okabe-Ito based, distinguishable for common color-vision deficiencies.
PALETTE = ("#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7", "#8DD3C7", "#FB8072")
OTHER = "other"
OTHER_COLOR = "#999999"

_NUMBERS = re.compile(r"\d+")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HEX_ID = re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{6,}\b")  # hashes, addresses: at least one digit


def pool_name(thread_name: str) -> str:
    """Thread pool a thread name belongs to: ids and counters replaced.

    ``sling-threadpool-e194d6fb-2b9e-47e6-8228-07a25d8a3d2e-(pool)-9`` -> ``sling-threadpool-*-(pool)-N``,
    ``qtp1996920089-84-acceptor-0@4bd6-ServerConnector@b30{HTTP/1.1}`` -> ``qtpN-N-acceptor-N``.
    """
    base = thread_name.split("{", 1)[0].split("@", 1)[0].strip()
    base = _HEX_ID.sub("*", _UUID.sub("*", base))
    return _NUMBERS.sub("N", base) or "(unnamed)"


_JVM_INTERNAL = re.compile(
    r"^(VM |GC |G1 |ParGC |Par |C1 |C2 |C\d |Compiler|Reference Handler|Finalizer|Signal Dispatcher|"
    r"Service Thread|Sweeper thread|Common-Cleaner|Attach Listener|Notification Thread|Monitor Deflation|"
    r"JFR |ZGC|ZDirector|ZStat|Shenandoah|Concurrent |Periodic GC|Surrogate Locker|Java2D Disposer|"
    r"process reaper|DestroyJavaVM)"
)
_FAMILY_TWO_WORDS = re.compile(r"([a-z][a-z0-9]*)([- ])([A-Za-z][A-Za-z]*)")
_FAMILY_WORD = re.compile(r"[A-Za-z][A-Za-z0-9/]*")


def family_name(thread_name: str) -> str:
    """Coarse family of a thread: "JVM internal", "Jetty (qtp)", "sling-threadpool", "OkHttp", ..."""
    if _JVM_INTERNAL.match(thread_name):
        return "JVM internal"
    pool = pool_name(thread_name)
    if pool.startswith("qtp"):
        return "Jetty (qtp)"
    if pool.startswith("pool-N-thread"):
        return "Executors (pool-N-thread-N)"
    if pool.startswith("Thread-N"):
        return "unnamed (Thread-N)"
    # "sling-threadpool-*-(...)" -> "sling-threadpool", "oak-lucene-N" -> "oak-lucene"
    two = _FAMILY_TWO_WORDS.match(pool)
    if two and two.group(3) != "N":
        return two.group(0)
    one = _FAMILY_WORD.match(pool)
    return one.group(0) if one else "(unnamed)"


_JDK_PREFIXES = ("java.", "javax.", "jdk.", "sun.", "com.sun.", "jakarta.")
# JDK-only stacks are identified by a characteristic frame (most are idle workers).
_JDK_MARKERS = (
    ("java.util.concurrent.ScheduledThreadPoolExecutor$DelayedWorkQueue.take", "idle: scheduled executor"),
    ("java.util.concurrent.ThreadPoolExecutor.getTask", "idle: executor worker (ThreadPoolExecutor)"),
    ("java.util.concurrent.ForkJoinPool.awaitWork", "idle: ForkJoinPool worker"),
    ("java.util.concurrent.ForkJoinPool.runWorker", "ForkJoinPool worker"),
    ("java.util.TimerThread.mainLoop", "idle: java.util.Timer"),
    ("java.lang.ref.", "JVM: reference handling"),
    ("java.net.SocketInputStream.socketRead0", "network read (JDK)"),
    ("sun.nio.ch.", "network / NIO (JDK)"),
    ("java.lang.Thread.sleep", "sleeping (JDK)"),
)


def _package(frame: str) -> str:
    """Package of a frame, shortened: "org.apache.jackrabbit.oak.x.Y.m(..)" -> "org.apache.jackrabbit"."""
    parts = frame.split("(", 1)[0].split(".")[:-2]  # drop class and method
    if not parts:
        return frame.split("(", 1)[0]
    first = parts[0]
    is_tld = len(first) <= 3 and first.isalpha()  # org, com, io, net, de, ...
    return ".".join(parts[:3] if is_tld else parts[:1])


def code_name(thread: ThreadInfo) -> str:
    """What code a thread runs: package of its top-most non-JDK frame."""
    if not thread.frames:
        return "JVM internal (no Java stack)"
    for frame in thread.frames:
        if not frame.startswith(_JDK_PREFIXES):
            return _package(frame)
    for marker, label in _JDK_MARKERS:
        if any(f.startswith(marker) for f in thread.frames):
            return label
    runnable = [f for f in thread.frames if not f.startswith("java.lang.Thread.run")]
    bottom = (runnable or thread.frames)[-1].split("(", 1)[0]
    return "JDK: " + ".".join(bottom.split(".")[-2:])


def group_of(thread: ThreadInfo, by: str) -> str | None:
    """Group a thread belongs to; ``None`` excludes it from the chart."""
    if by == "family":
        return "HTTP requests" if thread.request is not None else family_name(thread.name)
    if by == "code":
        return code_name(thread)
    if by == "state":
        return thread.state or "(VM internal)"
    if by == "request":
        if thread.request is None:
            return None
        return f"{thread.request.method} {thread.request.path.split('?', 1)[0]}"
    if by == "pool":
        if thread.request is not None:
            return "HTTP requests"
        return pool_name(thread.name)
    raise ValueError(f"unknown grouping: {by}")


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


@dataclass
class Point:
    time: datetime
    value: float  # thread count (count mode) or seconds (duration mode)
    group: str
    dump: ThreadDump
    threads: list[ThreadInfo]  # all threads behind the point (one in duration mode)
    duration_kind: str | None = None  # "request age" or "thread age"
    breakdown: dict[str, int] = field(default_factory=dict)  # count mode, "other": threads per merged group


@dataclass
class Series:
    group: str
    color: str
    total: int  # thread snapshots in this group
    points: list[Point] = field(default_factory=list)
    members: list[tuple[str, int]] = field(default_factory=list)  # "other": merged groups and their totals


@dataclass
class Chart:
    mode: str
    group_by: str
    series: list[Series]
    start: datetime | None
    end: datetime | None
    y_max: float
    dumps: int
    invalid_request_ages: int = 0  # request start after dump time: wrong time zone

    @property
    def other_groups(self) -> list[str]:
        """Groups merged into "other" (most threads first), e.g. to chart them on their own."""
        return [g for s in self.series if s.group == OTHER for g, _ in s.members]

    @property
    def points(self) -> list[Point]:
        return [p for s in self.series for p in s.points]

    @property
    def log_y(self) -> bool:
        return self.mode == "duration"

    def x_fraction(self, time: datetime) -> float:
        if self.start is None or self.end is None or self.start == self.end:
            return 0.5
        return (time - self.start).total_seconds() / (self.end - self.start).total_seconds()

    def y_fraction(self, value: float) -> float:
        if self.log_y:
            low, high = math.log10(0.1), math.log10(max(self.y_max, 1.0))
            return (math.log10(max(value, 0.1)) - low) / (high - low) if high > low else 0.0
        return value / self.y_max if self.y_max else 0.0

    def y_ticks(self) -> list[tuple[float, str]]:
        if self.log_y:
            marks = [(1, "1s"), (10, "10s"), (60, "1m"), (600, "10m"), (3600, "1h"), (36000, "10h"), (86400, "1d")]
            marks += [(86400 * 10, "10d"), (86400 * 100, "100d")]
            return [(v, label) for v, label in marks if v <= self.y_max * 1.5]
        step = max(1, math.ceil(self.y_max / 5))
        return [(float(v), str(v)) for v in range(0, int(self.y_max) + step, step)]


def _duration(dump: ThreadDump, thread: ThreadInfo, tz: tzinfo | None) -> tuple[float | None, str | None, bool]:
    """(seconds, kind, invalid) for a thread snapshot."""
    req = thread.request
    if req is not None and req.started_at_ms is not None and dump.timestamp is not None:
        ts = dump.timestamp.replace(tzinfo=tz) if tz else dump.timestamp
        age = ts.timestamp() - req.started_at_ms / 1000.0
        if age < -1:
            return None, None, True
        return max(age, 0.0), "request age", False
    if thread.elapsed_s is not None:
        return thread.elapsed_s, "thread age", False
    return None, None, False


def count_invalid_request_ages(dumps: Sequence[ThreadDump], tz: tzinfo | None) -> int:
    """Request threads that would start after their dump in time zone ``tz``."""
    return sum(_duration(d, t, tz)[2] for d in dumps for t in d.threads if t.request is not None)


def build_chart(
    dumps: Sequence[ThreadDump],
    flt: ThreadFilter | None = None,
    mode: str = "count",
    group_by: str = "pool",
    tz: tzinfo | None = None,
    max_groups: int = len(PALETTE),
    only_groups: Collection[str] | None = None,
) -> Chart:
    """Build the chart; ``only_groups`` keeps just these groups (to look inside "other")."""
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    flt = flt or ThreadFilter()
    timed = [d for d in dumps if d.timestamp is not None]

    members: dict[tuple[int, str], list[ThreadInfo]] = defaultdict(list)  # (dump index, group) -> threads
    totals: Counter[str] = Counter()
    for i, dump in enumerate(timed):
        for thread in dump.threads:
            group = group_of(thread, group_by) if flt.matches(thread) else None
            if group is not None and (only_groups is None or group in only_groups):
                members[(i, group)].append(thread)
                totals[group] += 1

    ranked = [g for g, _ in totals.most_common()]
    shown = ranked if len(ranked) <= max_groups else ranked[: max_groups - 1]
    color = {g: PALETTE[i % len(PALETTE)] for i, g in enumerate(shown)}
    rename = {g: (g if g in color else OTHER) for g in ranked}
    series = {g: Series(g, color[g], totals[g]) for g in shown}
    if len(shown) < len(ranked):
        merged = [(g, totals[g]) for g in ranked if g not in color]
        series[OTHER] = Series(OTHER, OTHER_COLOR, sum(n for _, n in merged), members=merged)

    invalid = 0
    for i, dump in enumerate(timed):
        assert dump.timestamp is not None
        per_series: dict[str, list[ThreadInfo]] = defaultdict(list)
        for group in ranked:
            per_series[rename[group]].extend(members.get((i, group), []))
        for name, s in series.items():
            threads = per_series.get(name, [])
            if mode == "count":
                point = Point(dump.timestamp, float(len(threads)), name, dump, threads)
                if s.members:
                    counts = ((g, len(members.get((i, g), []))) for g, _ in s.members)
                    point.breakdown = dict(sorted(((g, n) for g, n in counts if n), key=lambda gn: -gn[1]))
                s.points.append(point)
                continue
            for thread in threads:
                value, kind, bad = _duration(dump, thread, tz)
                invalid += bad
                if value is not None:
                    group = group_of(thread, group_by) or name
                    s.points.append(Point(dump.timestamp, value, group, dump, [thread], kind))

    values = [p.value for s in series.values() for p in s.points]
    return Chart(
        mode=mode,
        group_by=group_by,
        series=list(series.values()),
        start=timed[0].timestamp if timed else None,
        end=timed[-1].timestamp if timed else None,
        y_max=max(values, default=0.0) or 1.0,
        dumps=len(timed),
        invalid_request_ages=invalid,
    )


def describe(point: Point, max_frames: int = 8) -> str:
    """Plain-text details of a point, shown on hover."""
    when = point.time.strftime("%Y-%m-%d %H:%M:%S")
    if len(point.threads) != 1 or point.duration_kind is None:
        lines = [f"{point.group}: {len(point.threads)} thread(s) at {when}", f"dump: {point.dump.source}"]
        states = Counter(t.state or "(no state)" for t in point.threads)
        lines.append("states: " + (", ".join(f"{k}={v}" for k, v in states.most_common()) or "-"))
        if point.breakdown:
            lines.append(f"made of {len(point.breakdown)} group(s):")
            lines += [f"  {n:>5}  {g}" for g, n in list(point.breakdown.items())[:12]]
            if len(point.breakdown) > 12:
                lines.append(f"  ... {len(point.breakdown) - 12} more groups")
            lines.append("threads:")
        lines += [f"  {t.name}  [{t.state or '-'}]" for t in point.threads[:15]]
        if len(point.threads) > 15:
            lines.append(f"  ... {len(point.threads) - 15} more")
        return "\n".join(lines)

    t = point.threads[0]
    state = f"{t.state} ({t.state_detail})" if t.state_detail else (t.state or "-")
    lines = [
        t.name,
        f"group: {point.group}    state: {state}",
        f"dump:  {when}  {point.dump.source}",
        f"{point.duration_kind}: {format_duration(point.value)}",
    ]
    if point.duration_kind == "request age" and t.elapsed_s is not None:
        lines.append(f"thread age: {format_duration(t.elapsed_s)}")
    if t.cpu_ms is not None:
        lines.append(f"cpu: {t.cpu_ms:.0f}ms")
    lines += [f"  at {f}" for f in t.frames[:max_frames]]
    if len(t.frames) > max_frames:
        lines.append(f"  ... {len(t.frames) - max_frames} more frames")
    return "\n".join(lines)


# ------------------------------------------------------------------------------ HTML

_W, _H = 1100, 520
_LEFT, _RIGHT, _TOP, _BOTTOM = 70, 20, 20, 50


def _x(chart: Chart, time: datetime) -> float:
    return _LEFT + chart.x_fraction(time) * (_W - _LEFT - _RIGHT)


def _y(chart: Chart, value: float) -> float:
    return _H - _BOTTOM - chart.y_fraction(value) * (_H - _TOP - _BOTTOM)


HTML_BIN_PX = 3  # points of one series closer than this share a marker (keeps big exports small)


def _bins(chart: Chart, points: list[Point]) -> dict[tuple[float, float], list[Point]]:
    bins: dict[tuple[int, int], list[Point]] = defaultdict(list)
    for p in points:
        bins[(round(_x(chart, p.time) / HTML_BIN_PX), round(_y(chart, p.value) / HTML_BIN_PX))].append(p)
    out: dict[tuple[float, float], list[Point]] = {}
    for members in bins.values():
        members.sort(key=lambda p: -p.value)
        out[(_x(chart, members[0].time), _y(chart, members[0].value))] = members
    return out


def _tip(points: list[Point]) -> str:
    text = describe(points[0], max_frames=4)
    if len(points) > 1:
        others = [p.threads[0].name if len(p.threads) == 1 else p.group for p in points[1:6]]
        text = f"{len(points)} threads here\n\n{text}\n\nalso: " + "\n      ".join(others)
        if len(points) > 6:
            text += f"\n      ... {len(points) - 6} more"
    return text


def to_html(chart: Chart, title: str = "tdscope timeline") -> str:
    """Self-contained HTML page (inline SVG + a few lines of JS) with hover details."""
    esc = html.escape
    parts: list[str] = []
    for tick, label in chart.y_ticks():
        y = _y(chart, tick)
        parts.append(f'<line class="grid" x1="{_LEFT}" x2="{_W - _RIGHT}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="axis" x="{_LEFT - 8}" y="{y + 4:.1f}" text-anchor="end">{esc(label)}</text>')
    if chart.start and chart.end:
        span = (chart.end - chart.start).total_seconds()
        for k in range(6):
            t = chart.start + (chart.end - chart.start) * (k / 5)
            x = _x(chart, t)
            fmt = "%H:%M:%S" if span < 86400 else "%m-%d %H:%M"
            label = t.strftime(fmt)
            parts.append(f'<text class="axis" x="{x:.1f}" y="{_H - _BOTTOM + 20}" text-anchor="middle">{label}</text>')
    parts.append(
        f'<line class="frame" x1="{_LEFT}" x2="{_LEFT}" y1="{_TOP}" y2="{_H - _BOTTOM}"/>'
        f'<line class="frame" x1="{_LEFT}" x2="{_W - _RIGHT}" y1="{_H - _BOTTOM}" y2="{_H - _BOTTOM}"/>'
    )

    tips: list[str] = []
    for index, s in enumerate(chart.series):
        group_parts = [f'<g class="series" data-series="{index}">']
        if chart.mode == "count" and len(s.points) > 1:
            coords = " ".join(f"{_x(chart, p.time):.1f},{_y(chart, p.value):.1f}" for p in s.points)
            group_parts.append(f'<polyline points="{coords}" stroke="{s.color}" fill="none" stroke-width="2"/>')
        for (x, y), points in _bins(chart, s.points).items():
            tips.append(_tip(points))
            r = 4 if chart.mode == "count" else 3.5 if len(points) == 1 else 4.5
            group_parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{s.color}" data-tip="{len(tips) - 1}"/>'
            )
        group_parts.append("</g>")
        parts.append("".join(group_parts))

    def legend_title(s: Series) -> str:
        if not s.members:
            return ""
        lines = [f"{n}  {g}" for g, n in s.members[:30]]
        if len(s.members) > 30:
            lines.append(f"... {len(s.members) - 30} more groups")
        return f' title="{esc(chr(10).join(lines))}"'

    legend = "".join(
        f'<button class="key" data-series="{i}" aria-pressed="true"{legend_title(s)}>'
        f'<span class="swatch" style="background:{s.color}"></span>{esc(s.group)} <small>{s.total}</small></button>'
        for i, s in enumerate(chart.series)
    )
    if chart.other_groups:
        rows = "".join(f"<li><small>{n}</small> {esc(g)}</li>" for s in chart.series for g, n in s.members)
        legend += (
            f'<details class="other"><summary>what is in "other" ({len(chart.other_groups)} groups)</summary>'
            f"<ol>{rows}</ol></details>"
        )
    y_label = "threads" if chart.mode == "count" else "duration (log scale): request age, else thread age"
    note = ""
    if chart.invalid_request_ages:
        note = (
            f"<p class='note'>{chart.invalid_request_ages} request(s) seem to start after the dump time "
            "and were left out: wrong time zone?</p>"
        )
    tips_json = json.dumps(tips).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
:root {{ --bg:#ffffff; --fg:#1f2328; --muted:#6e7781; --grid:#e6e8eb; --panel:#f6f8fa; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0d1117; --fg:#e6edf3; --muted:#8b949e; --grid:#21262d; --panel:#161b22; }}
}}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg); font:14px system-ui, sans-serif; }}
h1 {{ font-size:18px; margin:0 0 4px; }} p {{ color:var(--muted); margin:0 0 12px; }}
svg {{ width:100%; height:auto; max-width:{_W}px; display:block; }}
.grid {{ stroke:var(--grid); }} .frame {{ stroke:var(--muted); }} .axis {{ fill:var(--muted); font-size:12px; }}
circle {{ stroke:var(--bg); stroke-width:1; cursor:pointer; }} circle:hover {{ stroke:var(--fg); stroke-width:2; }}
.legend {{ display:flex; flex-wrap:wrap; gap:6px; margin:10px 0; }}
.key {{ display:flex; align-items:center; gap:6px; border:1px solid var(--grid); background:var(--panel);
  color:var(--fg); border-radius:6px; padding:4px 8px; cursor:pointer; font:inherit; }}
.key[aria-pressed="false"] {{ opacity:.4; }} .swatch {{ width:12px; height:12px; border-radius:3px; }}
.key small {{ color:var(--muted); }} .hidden {{ display:none; }}
.other {{ flex-basis:100%; color:var(--muted); }} .other ol {{ columns:3 240px; margin:6px 0; font-size:12px; }}
.other small {{ display:inline-block; min-width:4em; text-align:right; margin-right:6px; }}
#tip {{ position:fixed; pointer-events:none; white-space:pre; font:12px ui-monospace, monospace;
  background:var(--panel); color:var(--fg); border:1px solid var(--grid); border-radius:6px; padding:8px;
  max-width:90vw; overflow:hidden; display:none; }}
.note {{ color:#D55E00; }}
</style></head><body>
<h1>{esc(title)}</h1>
<p>{chart.dumps} dump(s) · grouped by {esc(chart.group_by)} · Y: {esc(y_label)}
 · click a legend entry to hide it</p>
{note}
<div class="legend">{legend}</div>
<svg viewBox="0 0 {_W} {_H}" role="img" aria-label="{esc(title)}">{"".join(parts)}</svg>
<div id="tip"></div>
<script>
const tips = {tips_json};
const tip = document.getElementById("tip");
document.querySelectorAll("circle").forEach(c => {{
  c.addEventListener("mousemove", e => {{
    tip.textContent = tips[+c.dataset.tip]; tip.style.display = "block";
    const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    const y = Math.min(e.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
    tip.style.left = Math.max(8, x) + "px"; tip.style.top = Math.max(8, y) + "px";
  }});
  c.addEventListener("mouseleave", () => tip.style.display = "none");
}});
document.querySelectorAll(".key").forEach(k => k.addEventListener("click", () => {{
  const on = k.getAttribute("aria-pressed") !== "true";
  k.setAttribute("aria-pressed", on);
  document.querySelector(`g[data-series="${{k.dataset.series}}"]`).classList.toggle("hidden", !on);
}}));
</script></body></html>
"""
