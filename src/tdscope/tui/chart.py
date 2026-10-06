"""Terminal timeline chart: X = dump time, Y = thread count or duration, colored by group."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

from ..timeline import Chart, Point

Y_LABEL_WIDTH = 7
MARKER = "●"
LINE = "·"


@dataclass
class _Layout:
    width: int
    height: int  # rows of the plot area (without the two X axis rows)

    @property
    def plot_width(self) -> int:
        return max(self.width - Y_LABEL_WIDTH - 3, 1)  # 2 cells of right margin


class ChartWidget(Widget, can_focus=True):
    """Draws a :class:`Chart`; hovering (mouse) or moving the cursor (keys) selects points."""

    DEFAULT_CSS = """
    ChartWidget { height: 1fr; min-height: 10; }
    """
    BINDINGS = [
        Binding("left", "move(-1, 0)", "←", show=False),
        Binding("right", "move(1, 0)", "→", show=False),
        Binding("up", "move(0, -1)", "↑", show=False),
        Binding("down", "move(0, 1)", "↓", show=False),
        Binding("shift+left", "move(-8, 0)", show=False),
        Binding("shift+right", "move(8, 0)", show=False),
        Binding("n", "jump(1)", "next point"),
        Binding("p", "jump(-1)", "prev point"),
    ]

    chart: reactive[Chart | None] = reactive(None)
    hidden: reactive[frozenset[str]] = reactive(frozenset())
    cursor: reactive[tuple[int, int] | None] = reactive(None)

    class Hovered(Message):
        def __init__(self, points: list[Point]) -> None:
            super().__init__()
            self.points = points

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._cells: dict[tuple[int, int], list[Point]] = {}
        self._order: list[tuple[int, int]] = []

    # -- geometry ------------------------------------------------------------------

    def _layout(self) -> _Layout:
        return _Layout(width=self.size.width, height=max(self.size.height - 2, 1))

    def _cell(self, chart: Chart, layout: _Layout, point: Point) -> tuple[int, int]:
        col = Y_LABEL_WIDTH + 1 + round(chart.x_fraction(point.time) * (layout.plot_width - 1))
        row = round((1 - chart.y_fraction(point.value)) * (layout.height - 1))
        return col, min(max(row, 0), layout.height - 1)

    def _visible_series(self, chart: Chart) -> list[tuple[int, str]]:
        return [(i, s.group) for i, s in enumerate(chart.series) if s.group not in self.hidden]

    # -- rendering -----------------------------------------------------------------

    def render(self) -> Text:
        chart = self.chart
        layout = self._layout()
        if chart is None or not chart.series:
            return Text("no data to plot", style="dim")
        width = layout.width
        grid: list[list[tuple[str, Style | None]]] = [[(" ", None)] * width for _ in range(layout.height + 2)]

        # Y axis with tick labels
        axis_style = Style(dim=True)
        for row in range(layout.height):
            grid[row][Y_LABEL_WIDTH] = ("│", axis_style)
        for value, label in chart.y_ticks():
            row = round((1 - chart.y_fraction(value)) * (layout.height - 1))
            if 0 <= row < layout.height:
                for i, ch in enumerate(label.rjust(Y_LABEL_WIDTH - 1)[: Y_LABEL_WIDTH - 1]):
                    grid[row][i] = (ch, axis_style)
                grid[row][Y_LABEL_WIDTH] = ("┤", axis_style)

        # X axis with time labels
        base = layout.height
        for col in range(Y_LABEL_WIDTH, width):
            grid[base][col] = ("─", axis_style)
        grid[base][Y_LABEL_WIDTH] = ("└", axis_style)
        if chart.start and chart.end:
            span = (chart.end - chart.start).total_seconds()
            fmt = "%H:%M:%S" if span < 86400 else "%m-%d %H:%M"
            for frac in (0.0, 0.5, 1.0) if span else (0.5,):
                label = (chart.start + (chart.end - chart.start) * frac).strftime(fmt)
                col = Y_LABEL_WIDTH + 1 + round(frac * (layout.plot_width - 1))
                start = min(max(col - len(label) // 2, Y_LABEL_WIDTH + 1), width - len(label))
                for i, ch in enumerate(label):
                    if 0 <= start + i < width:
                        grid[base + 1][start + i] = (ch, axis_style)

        # lines (count mode), then markers on top
        self._cells = {}
        visible = self._visible_series(chart)
        for index, _ in visible:
            series = chart.series[index]
            style = Style(color=series.color)
            cells = [self._cell(chart, layout, p) for p in series.points]
            if chart.mode == "count":
                for (c1, r1), (c2, r2) in itertools.pairwise(cells):
                    steps = max(abs(c2 - c1), abs(r2 - r1), 1)
                    for k in range(1, steps):
                        c = c1 + round((c2 - c1) * k / steps)
                        r = r1 + round((r2 - r1) * k / steps)
                        if grid[r][c][0] == " ":
                            grid[r][c] = (LINE, style)
            for point, cell in zip(series.points, cells, strict=True):
                self._cells.setdefault(cell, []).append(point)
        for index, _ in visible:
            series = chart.series[index]
            style = Style(color=series.color, bold=True)
            for point in series.points:
                col, row = self._cell(chart, layout, point)
                grid[row][col] = (MARKER, style)
        self._order = sorted(self._cells)

        if self.cursor is not None:
            col, row = self.cursor
            if 0 <= row < layout.height and 0 <= col < width:
                ch, current = grid[row][col]
                grid[row][col] = (ch if ch.strip() else "+", (current or Style()) + Style(reverse=True))

        text = Text()
        for r, line in enumerate(grid):
            for ch, cell_style in line:
                text.append(ch, cell_style)
            if r < len(grid) - 1:
                text.append("\n")
        return text

    # -- interaction ---------------------------------------------------------------

    def points_near(self, col: int, row: int, radius: int = 2) -> list[Point]:
        best: tuple[int, tuple[int, int]] | None = None
        for c, r in self._cells:
            d = abs(c - col) + abs(r - row)
            if d <= radius and (best is None or d < best[0]):
                best = (d, (c, r))
        if best is None:
            return []
        return sorted(self._cells[best[1]], key=lambda p: -p.value)

    def _select(self, col: int, row: int) -> None:
        layout = self._layout()
        self.cursor = (min(max(col, Y_LABEL_WIDTH + 1), layout.width - 1), min(max(row, 0), layout.height - 1))
        points = self.points_near(*self.cursor)
        self.tooltip = None
        self.post_message(self.Hovered(points))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._select(int(event.x), int(event.y))

    def on_click(self, event: events.Click) -> None:
        self.focus()
        self._select(int(event.x), int(event.y))

    def action_move(self, dx: int, dy: int) -> None:
        col, row = self.cursor or (Y_LABEL_WIDTH + 1, self._layout().height // 2)
        self._select(col + dx, row + dy)

    def action_jump(self, step: int) -> None:
        if not self._order:
            return
        if self.cursor in self._cells:
            index = (self._order.index(self.cursor) + step) % len(self._order)
        else:
            index = 0 if step > 0 else len(self._order) - 1
        self._select(*self._order[index])

    def watch_chart(self) -> None:
        self.cursor = None
        self._cells = {}
        self._order = []
