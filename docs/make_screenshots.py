"""Regenerate the TUI screenshots in docs/screenshots (SVG) from the synthetic demo data.

    python docs/make_screenshots.py

Needs the `tui` extra. The demo dumps are generated into a temporary folder first.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from collections.abc import Callable
from datetime import timezone
from pathlib import Path

DOCS = Path(__file__).resolve().parent
sys.path.insert(0, str(DOCS / "demo"))

from make_demo_dumps import main as make_demo  # noqa: E402
from textual.pilot import Pilot  # noqa: E402
from textual.widgets import Input  # noqa: E402

from tdscope.timeline import Point  # noqa: E402
from tdscope.tui import TdscopeApp  # noqa: E402
from tdscope.tui.app import ChartScreen  # noqa: E402
from tdscope.tui.chart import ChartWidget  # noqa: E402
from tdscope.tui.screens import AnalysisForm, ExportDialog  # noqa: E402

OUT = DOCS / "screenshots"
SIZE = (150, 44)


async def until(app: TdscopeApp, pilot: Pilot[None], condition: Callable[[], bool]) -> None:
    for _ in range(600):
        if condition():
            await pilot.pause(0.2)
            return
        await pilot.pause(0.05)
        await app.workers.wait_for_complete()
    raise TimeoutError


def shot(app: TdscopeApp, name: str) -> None:
    app.save_screenshot(f"{name}.svg", str(OUT))
    print(f"  {name}.svg")


async def run_form(app: TdscopeApp, pilot: Pilot[None], key: str, command: str, **fields: str) -> None:
    await pilot.press(key)
    await until(app, pilot, lambda: isinstance(app.screen, AnalysisForm))
    for name, value in fields.items():
        app.screen.query_one(f"#f-{name}", Input).value = value
    await pilot.pause(0.3)
    await pilot.press("enter")
    await until(app, pilot, lambda: app.last is not None and app.last[0].command == command)


async def hover(pilot: Pilot[None], widget: ChartWidget, match: Callable[[Point], bool], at: float = 0.5) -> None:
    """Hover the plotted cell that matches, around the given fraction of the time axis."""
    cells = sorted(cell for cell, points in widget._cells.items() if any(match(p) for p in points))
    await pilot.hover(ChartWidget, offset=cells[min(int(len(cells) * at), len(cells) - 1)])
    await pilot.pause(0.3)


async def main(folder: Path) -> None:
    app = TdscopeApp(str(folder), tz=timezone.utc)
    async with app.run_test(size=SIZE) as pilot:
        await until(app, pilot, lambda: app.last is not None)
        table = app.query_one("#table")
        table.move_cursor(row=16)  # type: ignore[attr-defined]
        await pilot.pause(0.3)
        shot(app, "01-overview")

        await pilot.press("r")
        await until(app, pilot, lambda: isinstance(app.screen, AnalysisForm))
        app.screen.query_one("#f-match", Input).value = "io.wcm., com.example."
        await pilot.pause(0.3)
        shot(app, "02-analysis-form")
        app.screen.query_one("#f-match", Input).value = ""
        await pilot.pause(0.2)
        await pilot.press("enter")
        await until(app, pilot, lambda: app.last is not None and app.last[0].command == "requests")
        shot(app, "03-requests")

        await run_form(app, pilot, "l", "locks")
        shot(app, "04-locks")

        await pilot.press(":")
        await pilot.press(*"hotspots -s BLOCKED -t 5")
        await pilot.pause(0.3)
        shot(app, "05-command-line")
        await pilot.press("enter")
        await until(app, pilot, lambda: app.last is not None and app.last[0].command == "hotspots")

        await pilot.press("x")
        await until(app, pilot, lambda: isinstance(app.screen, ExportDialog))
        app.screen.query_one("#path", Input).value = "~/reports/blocked-hotspots.json"
        await pilot.pause(0.3)
        shot(app, "06-export")
        await pilot.press("escape")

        await pilot.press("g")
        await until(app, pilot, lambda: isinstance(app.screen, ChartScreen))
        screen = app.screen
        assert isinstance(screen, ChartScreen)
        widget = screen.query_one(ChartWidget)
        await pilot.press("b")  # family
        await pilot.pause(0.3)
        await hover(pilot, widget, lambda p: p.group == "HTTP requests" and p.value >= 15)
        shot(app, "07-timeline-count")

        await pilot.press("m", "b")  # duration, by code
        await pilot.pause(0.3)
        await hover(pilot, widget, lambda p: "reference.json" in p.threads[0].name, at=0.6)
        shot(app, "08-timeline-duration")

        await pilot.press("m", "b", "b", "b", "b")  # count mode, by family
        await pilot.pause(0.3)
        await hover(pilot, widget, lambda p: p.group == "other", at=0.4)
        shot(app, "09-timeline-other")

        await pilot.press("o")
        await pilot.pause(0.3)
        await hover(pilot, widget, lambda p: p.value > 0, at=0.3)
        shot(app, "10-timeline-other-expanded")


if __name__ == "__main__":
    folder = Path(tempfile.gettempdir()) / "tdscope-demo"
    shutil.rmtree(folder, ignore_errors=True)
    make_demo(folder)
    OUT.mkdir(exist_ok=True)
    print(f"writing screenshots to {OUT}")
    asyncio.run(main(folder))
