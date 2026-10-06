"""End-to-end tests of the TUI, driven headlessly through Textual's pilot."""

import asyncio
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import Checkbox, DataTable, Input, Select, Static

from tdscope.tui import TdscopeApp
from tdscope.tui.app import ChartScreen
from tdscope.tui.chart import ChartWidget
from tdscope.tui.screens import AnalysisForm, ExportDialog, OpenFolder

AEMCS = Path(__file__).parent / "fixtures" / "aemcs"
SIZE = (160, 48)


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _until(app, pilot, condition, timeout=10.0):
    """Wait for a UI state: screens and workers update asynchronously after a key or click."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "timed out waiting for the UI"
        await pilot.pause(0.05)
        await app.workers.wait_for_complete()


def _table(app) -> DataTable:
    return app.query_one("#table", DataTable)


def _details(app) -> str:
    return str(app.query_one("#details", Static).render())


async def test_opens_folder_from_command_line_with_summary():
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)  # folder loaded, summary shown
        assert len(app.dumps) == 7
        assert _table(app).row_count == 7
        assert _table(app).border_title == "summary[7]"
        assert "threads:       19" in _details(app)
        info = str(app.query_one("#info", Static).render())
        assert "Dumps:   7   JVMs: 3" in info


async def test_without_folder_asks_to_open_one():
    app = TdscopeApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("r")
        await pilot.pause()
        assert not isinstance(app.screen, AnalysisForm)  # nothing loaded yet
        assert "open a folder" in _details(app).lower()


async def test_open_folder_dialog(tmp_path):
    app = TdscopeApp()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, OpenFolder)
        app.screen.query_one("#path", Input).value = str(tmp_path / "missing")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, OpenFolder)  # error shown, dialog stays
        assert "no such file" in str(app.screen.query_one("#error", Static).render())
        app.screen.query_one("#path", Input).value = str(AEMCS)
        await pilot.press("enter")
        await _until(app, pilot, lambda: app.last is not None)
        assert app.folder == str(AEMCS) and len(app.dumps) == 7


@pytest.mark.parametrize(
    ("key", "command", "fields", "rows"),
    [
        ("s", "summary", {}, 7),
        ("r", "requests", {"tz": "UTC"}, 3),
        ("f", "frames", {"match": "io.wcm."}, 1),
        ("h", "hotspots", {"top": "4"}, 4),
        ("k", "stuck", {}, 1),
        ("l", "locks", {}, 2),
        ("c", "cpu", {"top": "1"}, 4),
    ],
)
async def test_every_analysis_has_a_key_and_a_form(key, command, fields, rows):
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)  # folder loaded, summary shown
        await pilot.press(key)
        await pilot.pause()
        form = app.screen
        assert isinstance(form, AnalysisForm) and form.command == command
        for name, value in fields.items():
            form.query_one(f"#f-{name}", Input).value = value
        await pilot.pause()
        assert f"tdscope {command}" in str(form.query_one("#preview", Static).render())
        await pilot.click("#run")
        await _until(app, pilot, lambda: app.last is not None and app.last[0].command == command)
        assert _table(app).row_count == rows
        assert _table(app).border_title.startswith(command)


async def test_form_keeps_values_and_rejects_invalid_input():
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)  # folder loaded, summary shown
        await pilot.press("f")
        await pilot.pause()
        await pilot.click("#run")  # frames without --match
        await pilot.pause()
        assert isinstance(app.screen, AnalysisForm)
        assert "--match" in str(app.screen.query_one("#error", Static).render())

        app.screen.query_one("#f-top_only", Checkbox).value = True
        match = app.screen.query_one("#f-match", Input)
        match.value = "io.wcm., oak"
        match.focus()
        await pilot.pause()  # the error message changed the layout
        await pilot.press("enter")  # Enter in any field submits the form
        await _until(app, pilot, lambda: app.last is not None and app.last[0].command == "frames")
        assert app.last[0].match == ["io.wcm.", "oak"] and app.last[0].top_only

        await pilot.press("f")
        await _until(app, pilot, lambda: isinstance(app.screen, AnalysisForm))
        assert app.screen.query_one("#f-match", Input).value == "io.wcm., oak"  # remembered


async def test_row_selection_updates_details():
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)  # folder loaded, summary shown
        app.run_command("requests", ["--tz", "UTC"])
        await _until(app, pilot, lambda: app.last is not None and app.last[0].command == "requests")
        assert "reference.json" in _details(app)
        await pilot.press("down")
        await pilot.pause()
        assert "GET /sites.html/content/example/en" in _details(app)


async def test_command_bar_runs_analyses_and_exports(tmp_path):
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)  # folder loaded, summary shown
        await pilot.press(":")
        await pilot.press(*"locks -t 1")
        await pilot.press("enter")
        await _until(app, pilot, lambda: app.last is not None and app.last[0].command == "locks")
        assert _table(app).row_count == 1

        out = tmp_path / "locks.json"
        await pilot.press(":")
        app.query_one("#command", Input).value = f"export {out}"
        await pilot.press("enter")
        await pilot.pause()
        data = json.loads(out.read_text())
        assert data["contentions"][0]["class_name"].endswith("CacheLIRS$Segment")

        await pilot.press(":")
        await pilot.press(*"nonsense", "enter")
        await pilot.pause()
        assert app.last[0].command == "locks"  # unknown command changes nothing


@pytest.mark.parametrize(("fmt", "check"), [("text", "seen 12x"), ("json", '"key": "POST')])
async def test_export_dialog(tmp_path, fmt, check):
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)  # folder loaded, summary shown
        app.run_command("requests", ["--tz", "UTC"])
        await _until(app, pilot, lambda: app.last[0].command == "requests")
        await pilot.press("x")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, ExportDialog)
        dialog.query_one("#format", Select).value = fmt
        await pilot.pause()
        target = tmp_path / f"out.{fmt}"
        dialog.query_one("#path", Input).value = str(target)
        await pilot.click("#export")
        await pilot.pause()
        assert not isinstance(app.screen, ExportDialog)
        assert check in target.read_text()
        if fmt == "json":
            assert len(json.loads(target.read_text())) == 3


@pytest.fixture
def warsaw_local_time(monkeypatch):
    """Run with a non-UTC local zone (CI runners use UTC, developers often do not)."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset() is not available on Windows")
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


async def test_timeline_chart(warsaw_local_time):
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)  # folder loaded, summary shown
        await pilot.press("g")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ChartScreen)
        widget = screen.query_one(ChartWidget)
        assert screen.zones[screen.zone_index][0] == "UTC"  # AEMaaCS dumps: local time gives negative ages
        assert screen.jvms[0][0].startswith("aem-author-pod-a_0_threaddumps (3 dumps")
        assert widget.chart.mode == "count" and widget.chart.dumps == 3

        legend = str(screen.query_one("#legend", Static).render())
        assert "1 ● HTTP requests" in legend

        await pilot.press("n")  # keyboard: jump to the first point
        await pilot.pause()
        hover = str(screen.query_one("#hover", Static).render())
        assert "thread(s) at 2026-04-26" in hover

        await pilot.press("m")  # duration mode
        await pilot.pause()
        assert widget.chart.mode == "duration" and widget.chart.invalid_request_ages == 0
        await pilot.press("n")
        await pilot.pause()
        assert "age:" in str(screen.query_one("#hover", Static).render())

        await pilot.press("1")  # hide the first group
        await pilot.pause()
        assert widget.hidden == {"HTTP requests"}

        await pilot.press("b")
        await pilot.pause()
        assert widget.chart.group_by == "family" and widget.hidden == frozenset()
        await pilot.press("b", "b")
        await pilot.pause()
        assert widget.chart.group_by == "state"

        await pilot.press("j", "j", "j")  # pod B, restarted pod A, all JVMs
        await pilot.pause()
        assert screen.jvms[screen.jvm_index][0] == "all 3 JVMs" and widget.chart.dumps == 7

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ChartScreen)


async def test_chart_mouse_hover_shows_thread_details():
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)  # folder loaded, summary shown
        await pilot.press("g", "m")
        await pilot.pause()
        screen = app.screen
        widget = screen.query_one(ChartWidget)
        # hover exactly over a plotted point
        col, row = next(iter(widget._cells))
        await pilot.hover(ChartWidget, offset=(col, row))
        await pilot.pause()
        assert widget.cursor == (col, row)
        hover = str(screen.query_one("#hover", Static).render())
        assert "group:" in hover and "dump:" in hover


async def test_chart_html_export(tmp_path):
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)  # folder loaded, summary shown
        await pilot.press("g", "e")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, ExportDialog)
        target = tmp_path / "chart.html"
        dialog.query_one("#path", Input).value = str(target)
        await pilot.click("#export")
        await pilot.pause()
        page = target.read_text()
        assert page.startswith("<!doctype html>") and "<svg" in page and "HTTP requests" in page


async def test_export_format_follows_file_extension(tmp_path):
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)
        await pilot.press("x")
        await _until(app, pilot, lambda: isinstance(app.screen, ExportDialog))
        target = tmp_path / "summary.json"
        app.screen.query_one("#path", Input).value = str(target)
        await pilot.pause()
        assert app.screen.query_one("#format", Select).value == "json"
        await pilot.press("enter")
        await _until(app, pilot, lambda: target.exists())
        assert len(json.loads(target.read_text())) == 7


async def test_chart_expands_other():
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)
        await pilot.press("g", "j", "j", "j")  # all JVMs, grouped by pool: more groups than colors
        await pilot.pause()
        screen = app.screen
        widget = screen.query_one(ChartWidget)
        merged = widget.chart.other_groups
        assert merged
        legend = str(screen.query_one("#legend", Static).render())
        assert f"└ {widget.chart.series[-1].members[0][1]:>6} {merged[0]}" in legend
        assert "o: chart them on their own" in legend

        await pilot.press("o")
        await pilot.pause()
        assert screen.drill == [merged]
        assert {s.group for s in widget.chart.series} == set(merged)  # few enough to all get a color
        assert "> other(" in str(screen.query_one("#chart-title", Static).render())

        await pilot.press("o")  # nothing left to expand
        await pilot.pause()
        assert len(screen.drill) == 1

        await pilot.press("escape")  # back to the full chart, still on the graph
        await pilot.pause()
        assert isinstance(app.screen, ChartScreen) and screen.drill == []
        assert widget.chart.other_groups == merged

        await pilot.press("o", "b")  # changing the grouping leaves the drill-down
        await pilot.pause()
        assert screen.drill == []

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ChartScreen)


async def test_command_line_export_creates_folders(tmp_path):
    app = TdscopeApp(str(AEMCS))
    async with app.run_test(size=SIZE) as pilot:
        await _until(app, pilot, lambda: app.last is not None)
        target = tmp_path / "new" / "folder" / "summary.txt"
        await pilot.press(":")
        app.query_one("#command", Input).value = f"export {target}"
        await pilot.press("enter")
        await _until(app, pilot, lambda: target.exists())
        assert "threads:" in target.read_text()
