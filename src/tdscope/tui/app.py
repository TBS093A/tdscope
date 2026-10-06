"""k9s-style terminal UI for tdscope."""

from __future__ import annotations

import argparse
import io
import shlex
from collections.abc import Sequence
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static

from .. import __version__
from ..analysis import by_jvm
from ..cli import COMMANDS, CommandError, parse_analysis, run_analysis, write_result
from ..loader import DEFAULT_PATTERNS, load
from ..model import ThreadDump
from ..timeline import (
    GROUPINGS,
    MODES,
    Chart,
    Point,
    build_chart,
    count_invalid_request_ages,
    describe,
    format_duration,
    to_html,
)
from ..views import Table, to_table
from .chart import ChartWidget
from .screens import COMMAND_FIELDS, COMMON_FIELDS, AnalysisForm, ExportDialog, OpenFolder

LOGO = r"""  _      _
 | |_ __| |___ __ ___ _ __  ___
 |  _/ _` (_-</ _/ _ \ '_ \/ -_)
  \__\__,_/__/\__\___/ .__/\___|
                     |_|"""

# key -> analysis; shown in the header like k9s shortcuts
ANALYSIS_KEYS = {
    "s": "summary",
    "r": "requests",
    "f": "frames",
    "h": "hotspots",
    "k": "stuck",
    "l": "locks",
    "c": "cpu",
}
MAX_CELL = 120

CSS = """
Screen { background: $surface; }
#top { height: 7; padding: 0 1; }
#info { width: 1fr; }
#keys { width: 2fr; }
#logo { width: 34; color: $accent; }
#command { display: none; margin: 0 1; border: tall $accent; }
#command.visible { display: block; }
#table { height: 2fr; border: round $primary; border-title-color: $accent; border-title-style: bold; }
#details-box { height: 1fr; border: round $secondary; border-title-color: $secondary; }
#details { padding: 0 1; }
AnalysisForm, OpenFolder, ExportDialog { align: center middle; }
#dialog { width: 90; max-width: 95%; height: auto; max-height: 90%; border: thick $accent; background: $panel;
          padding: 1 2; }
#dialog #fields { height: auto; max-height: 24; }
#dialog DirectoryTree { height: 16; }
#dialog #preview { margin-top: 1; }
#dialog #buttons { height: auto; margin-top: 1; }
#dialog Button { margin-right: 2; }
ChartScreen #chart-title { height: auto; padding: 0 1; background: $primary-background; }
ChartScreen #body { height: 1fr; }
ChartScreen ChartWidget { width: 1fr; height: 1fr; }
ChartScreen #side { width: 56; border-left: solid $secondary; }
ChartScreen #legend { height: auto; padding: 0 1; border-bottom: solid $secondary; }
ChartScreen #hover-box { height: 1fr; }
ChartScreen #hover { padding: 0 1; }
"""


def _short(text: str, root: str | None) -> str:
    """Strip the opened folder from file paths inside ``text``."""
    return text.replace(root.rstrip("/") + "/", "") if root else text


def _span(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start:%Y-%m-%d %H:%M} - {end:%H:%M}"
    return f"{start:%Y-%m-%d %H:%M} - {end:%Y-%m-%d %H:%M}"


def _display_path(path: str | None, width: int = 44) -> str:
    if not path:
        return "-"
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home) :]
    return path if len(path) <= width else "…" + path[-(width - 1) :]


class ChartScreen(Screen[None]):
    """Threads over time, colored by group, with hover details."""

    BINDINGS = [
        Binding("escape,q", "back", "back"),
        Binding("m", "mode", "mode"),
        Binding("o", "expand_other", "expand other"),
        Binding("b", "group_by", "group by"),
        Binding("j", "jvm", "JVM"),
        Binding("z", "timezone", "time zone"),
        Binding("e", "export", "export HTML"),
        *[Binding(str(i), f"toggle_group({i})", show=False) for i in range(1, 10)],
    ]

    def __init__(self, dumps: Sequence[ThreadDump], root: str | None, tz: tzinfo | None) -> None:
        super().__init__()
        self.root = root
        series = sorted(by_jvm(dumps), key=lambda s: (-len(s), s[0].timestamp or 0))
        self.jvms: list[tuple[str, list[ThreadDump]]] = [(self._jvm_label(s), s) for s in series]
        if len(series) > 1:
            self.jvms.append((f"all {len(series)} JVMs", list(dumps)))
        self.jvm_index = 0
        self.mode = "count"
        self.group_index = 0
        self.zones: list[tuple[str, tzinfo | None]] = [("local", None), ("UTC", timezone.utc)]
        if tz is not None:
            self.zones.insert(0, (str(tz), tz))
        self.zone_index = 0
        self.zone_explicit = tz is not None
        self.chart: Chart | None = None
        self.drill: list[list[str]] = []  # groups charted on their own after "expand other", per level

    def _jvm_label(self, series: list[ThreadDump]) -> str:
        first, last = series[0], series[-1]
        where = str(Path(_short(first.source, self.root)).parent)
        when = ""
        if first.timestamp and last.timestamp:
            when = f" {first.timestamp:%m-%d %H:%M}-{last.timestamp:%H:%M}"
        return f"{where} ({len(series)} dumps{when})"

    def compose(self) -> ComposeResult:
        yield Static(id="chart-title")
        with Horizontal(id="body"):
            yield ChartWidget(id="chart")
            with Vertical(id="side"):
                yield Static(id="legend")
                with VerticalScroll(id="hover-box"):
                    yield Static(Text("hover a point (mouse) or use ←↑→↓ / n p", style="dim"), id="hover")
        yield Footer()

    def on_mount(self) -> None:
        if not self.zone_explicit:
            self._guess_zone()
        self._rebuild()
        self.query_one(ChartWidget).focus()

    def _guess_zone(self) -> None:
        """Pick the first zone in which no request starts after its dump (AEMaaCS runs in UTC)."""
        dumps = self.jvms[-1][1]  # every dump (the last entry is "all JVMs" or the only JVM)
        invalid = [count_invalid_request_ages(dumps, z) for _, z in self.zones]
        if invalid[0] and 0 in invalid:
            self.zone_index = invalid.index(0)
            self.notify(f"time zone set to {self.zones[self.zone_index][0]}: request ages are negative in local time")

    @property
    def group_by(self) -> str:
        return GROUPINGS[self.group_index]

    def _rebuild(self) -> None:
        label, dumps = self.jvms[self.jvm_index]
        zone_label, zone = self.zones[self.zone_index]
        only = self.drill[-1] if self.drill else None
        self.chart = build_chart(dumps, mode=self.mode, group_by=self.group_by, tz=zone, only_groups=only)
        widget = self.query_one(ChartWidget)
        widget.hidden = frozenset()
        widget.chart = self.chart
        y = "threads" if self.mode == "count" else "duration, log scale (request age, else thread age)"
        title = Text.assemble(
            ("timeline ", "bold"),
            (f"mode={self.mode}  by={self.group_by}", ""),
            ("".join(f" > other({len(level)})" for level in self.drill), "bold yellow"),
            (f"  tz={zone_label}  ", ""),
            (f"JVM: {label}  ", "italic"),
            (f"Y: {y}", "dim"),
        )
        if self.chart.invalid_request_ages:
            title.append(f"  ⚠ {self.chart.invalid_request_ages} request ages < 0: press z", style="bold yellow")
        self.query_one("#chart-title", Static).update(title)
        self._legend()

    def _legend(self) -> None:
        widget = self.query_one(ChartWidget)
        text = Text()
        assert self.chart is not None
        for i, s in enumerate(self.chart.series, start=1):
            dim = s.group in widget.hidden
            key = str(i) if i <= 9 else " "
            text.append(f"{key} ", style="dim")
            text.append("● ", style="dim" if dim else f"bold {s.color}")
            text.append(f"{s.group[:40]} ", style="dim strike" if dim else "")
            text.append(f"{s.total}\n", style="dim")
            for group, total in s.members[:6]:
                text.append(f"    └ {total:>6} ", style="dim")
                text.append(f"{group[:38]}\n")
            if len(s.members) > 6:
                text.append(f"    └ ... {len(s.members) - 6} more groups\n", style="dim")
            if s.members:
                text.append("    o: chart them on their own\n", style="bold yellow")
        hints = "1-9 show/hide · m mode · b group by · j JVM"
        text.append(hints + (" · esc: back from other" if self.drill else ""), style="dim")
        self.query_one("#legend", Static).update(text)

    @on(ChartWidget.Hovered)
    def _hovered(self, event: ChartWidget.Hovered) -> None:
        panels = self.query("#hover").results(Static)  # empty while the screen is closing
        hover = next(panels, None)
        if hover is None:
            return
        points = event.points
        if not points:
            hover.update(Text("no point here", style="dim"))
            return
        text = Text()
        if len(points) > 1:
            text.append(f"{len(points)} points in this cell\n\n", style="bold")
        text.append(_short(describe(points[0]), self.root))
        if len(points) > 1:
            text.append("\n\nalso here:\n", style="bold")
            for p in points[1:11]:
                name = p.threads[0].name if len(p.threads) == 1 else f"{p.group}: {len(p.threads)} threads"
                value = format_duration(p.value) if p.duration_kind else f"{p.value:.0f}"
                text.append(f"  {value:>8}  {name}\n")
            if len(points) > 11:
                text.append(f"  ... {len(points) - 11} more\n", style="dim")
        hover.update(text)
        widget = self.query_one(ChartWidget)
        widget.tooltip = Text(_tooltip(points[0]))

    def action_mode(self) -> None:
        self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
        self._rebuild()

    def action_group_by(self) -> None:
        self.group_index = (self.group_index + 1) % len(GROUPINGS)
        self.drill = []
        self._rebuild()

    def action_jvm(self) -> None:
        self.jvm_index = (self.jvm_index + 1) % len(self.jvms)
        self.drill = []
        self._rebuild()

    def action_expand_other(self) -> None:
        """Chart only the groups merged into "other", each with its own color."""
        if self.chart is None or not self.chart.other_groups:
            self.notify("no 'other' group on this chart")
            return
        self.drill.append(self.chart.other_groups)
        self._rebuild()

    def action_back(self) -> None:
        if self.drill:
            self.drill.pop()
            self._rebuild()
        else:
            self.app.pop_screen()

    def action_timezone(self) -> None:
        self.zone_index = (self.zone_index + 1) % len(self.zones)
        self._rebuild()

    def action_toggle_group(self, index: int) -> None:
        if self.chart is None or index > len(self.chart.series):
            return
        widget = self.query_one(ChartWidget)
        group = self.chart.series[index - 1].group
        widget.hidden = widget.hidden ^ {group}
        self._legend()

    def action_export(self) -> None:
        def writer(path: str, fmt: str, _stacks: bool) -> None:
            assert self.chart is not None
            Path(path).write_text(to_html(self.chart, f"tdscope timeline: {self.jvms[self.jvm_index][0]}"))

        name = f"tdscope-timeline-{self.mode}-{self.group_by}"
        self.app.push_screen(
            ExportDialog("Export chart", ["html"], name, writer),
            lambda path: self.notify(f"saved {path}") if path else None,
        )


def _tooltip(point: Point) -> str:
    if point.duration_kind:
        return f"{point.threads[0].name}\n{point.group}\n{point.duration_kind}: {format_duration(point.value)}"
    text = f"{point.group}: {len(point.threads)} threads"
    for group, count in list(point.breakdown.items())[:3]:
        text += f"\n  {count:>4}  {group}"
    return text


class TdscopeApp(App[None]):
    TITLE = "tdscope"
    CSS = CSS
    BINDINGS = [
        Binding("o", "open", "open"),
        *[Binding(key, f"analysis('{cmd}')", cmd) for key, cmd in ANALYSIS_KEYS.items()],
        Binding("g", "chart", "graph"),
        Binding("x", "export", "export"),
        Binding(":", "command", "command"),
        Binding("escape", "hide_command", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(
        self, folder: str | None = None, tz: tzinfo | None = None, patterns: Sequence[str] = DEFAULT_PATTERNS
    ) -> None:
        super().__init__()
        self.folder = str(Path(folder).expanduser().resolve()) if folder else None
        self.tz = tz
        self.patterns = patterns
        self.dumps: list[ThreadDump] = []
        self.form_values: dict[str, dict[str, Any]] = {}
        self.last: tuple[argparse.Namespace, Any] | None = None
        self.table: Table | None = None

    # -- layout ----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static(id="info")
            yield Static(self._keys_text(), id="keys")
            yield Static(Text(LOGO), id="logo")
        yield Input(
            placeholder=":requests -m io.wcm. --tz UTC   |   :open PATH   |   :export FILE [json]",
            id="command",
            disabled=True,  # enabled while visible, so it never steals the focus
        )
        table: DataTable[Text] = DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield table
        with VerticalScroll(id="details-box"):
            yield Static(id="details")
        yield Footer()

    def _keys_text(self) -> Text:
        entries = [("o", "open folder"), *((k, v) for k, v in ANALYSIS_KEYS.items())]
        entries += [("g", "timeline graph"), ("x", "export output"), (":", "command"), ("q", "quit")]
        text = Text()
        per_column = 6
        columns = [entries[i : i + per_column] for i in range(0, len(entries), per_column)]
        for row in range(per_column):
            for col in columns:
                if row < len(col):
                    key, label = col[row]
                    text.append(f"<{key}>", style="bold magenta")
                    text.append(f" {label:<16}")
            text.append("\n")
        return text

    def on_mount(self) -> None:
        self.query_one("#table").border_title = "tdscope"
        self.query_one("#details-box").border_title = "details"
        self._update_info()
        self.query_one("#table").focus()
        if self.folder:
            self.load_folder(self.folder)
        else:
            self._details("Press <o> to open a folder with thread dumps.")

    def _update_info(self) -> None:
        text = Text()
        text.append("Folder:  ", style="bold")
        text.append(f"{_display_path(self.folder)}\n")
        jvms = len(by_jvm(self.dumps)) if self.dumps else 0
        threads = sum(len(d.threads) for d in self.dumps)
        text.append("Dumps:   ", style="bold")
        text.append(f"{len(self.dumps)}   JVMs: {jvms}   threads: {threads}\n")
        timed = [d.timestamp for d in self.dumps if d.timestamp]
        text.append("Span:    ", style="bold")
        text.append(f"{_span(min(timed), max(timed))}\n" if timed else "-\n")
        text.append("TZ:      ", style="bold")
        text.append(f"{self.tz or 'local'}\n")
        text.append(f"tdscope {__version__}", style="dim")
        self.query_one("#info", Static).update(text)

    def _details(self, text: str | Text) -> None:
        # query() instead of query_one(): late events may arrive while the app shuts down
        for panel in self.query("#details").results(Static):
            panel.update(text if isinstance(text, Text) else Text(text))

    # -- loading ---------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="load")
    def load_folder(self, folder: str) -> None:
        self.call_from_thread(self._loading, folder)
        warnings = io.StringIO()
        try:
            dumps = load([folder], patterns=self.patterns, warn=warnings)
        except OSError as exc:
            self.call_from_thread(self._load_failed, str(exc))
            return
        self.call_from_thread(self._loaded, folder, dumps)

    def _loading(self, folder: str) -> None:
        self.query_one("#table").loading = True
        self._details(f"Loading {folder} ...")

    def _load_failed(self, message: str) -> None:
        self.query_one("#table").loading = False
        self.notify(message, severity="error")

    def _loaded(self, folder: str, dumps: list[ThreadDump]) -> None:
        self.folder, self.dumps = folder, dumps
        self.query_one("#table").loading = False
        self._update_info()
        if not dumps:
            self.notify(f"no thread dumps found in {folder}", severity="warning")
            self._details("No thread dumps found.")
            return
        self.notify(f"loaded {len(dumps)} dump(s)")
        self.run_command("summary", [])

    # -- analyses --------------------------------------------------------------------

    def _need_dumps(self) -> bool:
        if not self.dumps or not self.folder:
            self.notify("open a folder with thread dumps first (o)", severity="warning")
            return False
        return True

    def action_analysis(self, command: str) -> None:
        if not self._need_dumps():
            return
        assert self.folder is not None
        values = self.form_values.setdefault(command, {})
        if command == "requests" and self.tz is not None and "tz" not in values:
            values["tz"] = str(self.tz)

        def done(args: list[str] | None) -> None:
            if args is not None:
                self.form_values[command] = form.values
                self.run_command(command, args)

        form = AnalysisForm(command, values, self.folder)
        self.push_screen(form, done)

    def run_command(self, command: str, args: list[str]) -> None:
        if not self._need_dumps():
            return
        assert self.folder is not None
        try:
            opts = parse_analysis([command, *args, self.folder])
        except CommandError as exc:
            self.notify(str(exc).strip() or "invalid command", severity="error", timeout=8)
            return
        self.query_one("#table").loading = True
        self._run(opts)

    @work(thread=True, exclusive=True, group="analysis")
    def _run(self, opts: argparse.Namespace) -> None:
        try:
            result = run_analysis(opts, self.dumps)
        except Exception as exc:  # surface any analysis error instead of crashing the UI
            self.call_from_thread(self._load_failed, f"{opts.command} failed: {exc}")
            return
        self.call_from_thread(self._show, opts, result)

    def _show(self, opts: argparse.Namespace, result: Any) -> None:
        self.last = (opts, result)
        self.table = to_table(opts.command, result, self.folder)
        widget: DataTable[Text] = self.query_one("#table", DataTable)
        widget.loading = False
        widget.clear(columns=True)
        widget.add_columns(*self.table.columns)
        for row in self.table.rows:
            widget.add_row(*(Text(c if len(c) <= MAX_CELL else c[: MAX_CELL - 1] + "…") for c in row))
        args = shlex.join(_args_of(opts))
        widget.border_title = (
            f"{opts.command}({args})[{len(self.table.rows)}]" if args else (f"{opts.command}[{len(self.table.rows)}]")
        )
        if self.table.rows:
            widget.move_cursor(row=0)
            self._details(self.table.details[0])
            widget.focus()
        else:
            self._details("No results.")

    @on(DataTable.RowHighlighted, "#table")
    def _row(self, event: DataTable.RowHighlighted) -> None:
        if self.table and 0 <= event.cursor_row < len(self.table.details):
            self._details(self.table.details[event.cursor_row])

    # -- command line ----------------------------------------------------------------

    def action_command(self) -> None:
        bar = self.query_one("#command", Input)
        bar.disabled = False
        bar.add_class("visible")
        bar.value = ""
        bar.focus()

    def action_hide_command(self) -> None:
        bar = self.query_one("#command", Input)
        if bar.has_class("visible"):
            bar.remove_class("visible")
            bar.disabled = True
            self.query_one("#table").focus()

    @on(Input.Submitted, "#command")
    def _command_submitted(self, event: Input.Submitted) -> None:
        self.action_hide_command()
        try:
            words = shlex.split(event.value.strip().lstrip(":"))
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        if not words:
            return
        head, rest = words[0], words[1:]
        if head in ("q", "quit"):
            self.exit()
        elif head in ("o", "open"):
            if rest:
                self.load_folder(str(Path(rest[0]).expanduser().resolve()))
            else:
                self.action_open()
        elif head in ("x", "export"):
            if not rest:
                self.action_export()
            else:
                fmt = rest[1] if len(rest) > 1 else ("json" if rest[0].endswith(".json") else "text")
                self._export_to(rest[0], fmt, True)
        elif head in ("g", "graph", "timeline"):
            self.action_chart()
        elif head in COMMANDS:
            self.run_command(head, rest)
        else:
            self.notify(f"unknown command: {head}", severity="error")

    # -- open / export / chart -------------------------------------------------------

    def action_open(self) -> None:
        def done(path: str | None) -> None:
            if path:
                self.load_folder(path)

        self.push_screen(OpenFolder(self.folder or str(Path.cwd())), done)

    def _export_to(self, path: str, fmt: str, with_stacks: bool) -> None:
        if self.last is None:
            self.notify("run an analysis first", severity="warning")
            return
        opts, result = self.last
        target = Path(path).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as out:
                write_result(opts.command, result, out, fmt, show_stack=with_stacks)
        except OSError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"saved {target}")

    def action_export(self) -> None:
        if self.last is None:
            self.notify("run an analysis first", severity="warning")
            return
        opts, result = self.last

        def writer(path: str, fmt: str, with_stacks: bool) -> None:
            with open(path, "w", encoding="utf-8") as out:
                write_result(opts.command, result, out, fmt, show_stack=with_stacks)

        self.push_screen(
            ExportDialog(f"Export {opts.command} output", ["text", "json"], f"tdscope-{opts.command}", writer),
            lambda path: self.notify(f"saved {path}") if path else None,
        )

    def action_chart(self) -> None:
        if self._need_dumps():
            self.push_screen(ChartScreen(self.dumps, self.folder, self.tz))


def _args_of(opts: argparse.Namespace) -> list[str]:
    """Non-default options of a parsed analysis, for the table title."""
    out: list[str] = []
    for f in COMMON_FIELDS + COMMAND_FIELDS[opts.command]:
        value = getattr(opts, f.dest, None)
        if f.kind == "bool" and value:
            out.append(f.flag)
        elif f.kind == "list":
            for v in value or []:
                out += [f.flag, v]
        elif f.kind in ("str", "int") and value is not None and f.dest not in ("depth", "min_dumps"):
            out += [f.flag, str(value)]
    return out


def run_tui(folder: str | None = None, tz: tzinfo | None = None, patterns: Sequence[str] = DEFAULT_PATTERNS) -> int:
    TdscopeApp(folder, tz=tz, patterns=patterns).run()
    return 0
