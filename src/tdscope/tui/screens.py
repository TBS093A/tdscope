"""Modal dialogs: analysis parameters, open folder, export."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DirectoryTree, Input, Label, Select, Static

from ..cli import COMMANDS, CommandError, parse_analysis


@dataclass(frozen=True)
class Field:
    dest: str
    flag: str
    label: str
    kind: str  # "list" (comma separated, repeated flag), "str", "int" or "bool"
    placeholder: str = ""


COMMON_FIELDS = (
    Field("match", "-m", "Frames containing (comma separated)", "list", "io.wcm., com.mycompany."),
    Field("state", "-s", "Thread states (comma separated)", "list", "BLOCKED, RUNNABLE"),
    Field("name", "-n", "Thread name regex", "str", "^qtp"),
    Field("top", "-t", "Show only first N results", "int", "all"),
    Field("regex", "-E", "--match patterns are regular expressions", "bool"),
    Field("http_only", "--http-only", "Only threads serving an HTTP request", "bool"),
)

COMMAND_FIELDS: dict[str, tuple[Field, ...]] = {
    "summary": (),
    "requests": (
        Field("tz", "--tz", "JVM time zone (AEM as a Cloud Service: UTC)", "str", "local"),
        Field("keep_query", "--keep-query", "Keep the query string when grouping", "bool"),
    ),
    "frames": (Field("top_only", "--top-only", "Count only the top-most matching frame", "bool"),),
    "hotspots": (Field("depth", "--depth", "Stack frames compared", "int", "10"),),
    "stuck": (
        Field("min_dumps", "--min-dumps", "Minimum consecutive dumps", "int", "3"),
        Field("depth", "--depth", "Stack frames compared", "int", "20"),
        Field("all_states", "--all-states", "Include idle threads (WAITING / TIMED_WAITING)", "bool"),
        Field("include_network_wait", "--include-network-wait", "Include background socket reads", "bool"),
    ),
    "locks": (),
    "cpu": (),
}


def build_args(fields: tuple[Field, ...], values: dict[str, Any]) -> list[str]:
    """CLI arguments for form values (shared by the form and its tests)."""
    args: list[str] = []
    for f in fields:
        value = values.get(f.dest)
        if f.kind == "bool":
            if value:
                args.append(f.flag)
        elif f.kind == "list":
            for item in (v.strip() for v in str(value or "").split(",")):
                if item:
                    args += [f.flag, item]
        elif value not in (None, ""):
            args += [f.flag, str(value).strip()]
    return args


class AnalysisForm(ModalScreen[list[str] | None]):
    """Parameters of one analysis; returns the CLI arguments (without paths)."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, command: str, values: dict[str, Any], folder: str) -> None:
        super().__init__()
        self.command = command
        self.fields = COMMON_FIELDS + COMMAND_FIELDS[command]
        self.values = dict(values)
        self.folder = folder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(f"{self.command}: {COMMANDS[self.command][0]}", style="bold"))
            with VerticalScroll(id="fields"):
                for f in self.fields:
                    if f.kind == "bool":
                        yield Checkbox(f.label, bool(self.values.get(f.dest)), id=f"f-{f.dest}")
                    else:
                        yield Label(f.label)
                        restrict = r"[0-9]*" if f.kind == "int" else None
                        yield Input(
                            str(self.values.get(f.dest) or ""),
                            placeholder=f.placeholder,
                            id=f"f-{f.dest}",
                            restrict=restrict,
                        )
            yield Static(id="preview")
            yield Static(id="error")
            with Horizontal(id="buttons"):
                yield Button("Run", variant="primary", id="run")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._refresh_preview()
        inputs = self.query(Input)
        if inputs:
            inputs.first().focus()

    def _collect(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for f in self.fields:
            widget = self.query_one(f"#f-{f.dest}")
            values[f.dest] = widget.value  # type: ignore[attr-defined]
        return values

    def _args(self) -> list[str]:
        return build_args(self.fields, self._collect())

    def _refresh_preview(self) -> None:
        cli = shlex.join(["tdscope", self.command, *self._args(), self.folder])
        self.query_one("#preview", Static).update(Text(cli, style="dim"))

    @on(Input.Changed)
    @on(Checkbox.Changed)
    def _changed(self) -> None:
        self._refresh_preview()

    @on(Input.Submitted)
    @on(Button.Pressed, "#run")
    def action_run(self) -> None:
        args = self._args()
        try:
            parse_analysis([self.command, *args, self.folder])
        except CommandError as exc:
            self.query_one("#error", Static).update(Text(str(exc).strip(), style="bold red"))
            return
        self.values.update(self._collect())
        self.dismiss(args)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class _FolderTree(DirectoryTree):
    def filter_paths(self, paths: Any) -> list[Path]:
        return [p for p in paths if not p.name.startswith(".")]


class OpenFolder(ModalScreen[str | None]):
    """Choose a folder (or file) with thread dumps."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, start: str) -> None:
        super().__init__()
        self.start = start

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text("Open thread dumps: type a path or pick a folder, Enter opens", style="bold"))
            yield Input(self.start, id="path")
            base = Path(self.start).expanduser()
            yield _FolderTree(str(base if base.is_dir() else base.parent if base.parent.is_dir() else Path.cwd()))
            yield Static(id="error")
            with Horizontal(id="buttons"):
                yield Button("Open", variant="primary", id="open")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#path", Input).focus()

    @on(DirectoryTree.DirectorySelected)
    @on(DirectoryTree.FileSelected)
    def _picked(self, event: DirectoryTree.DirectorySelected | DirectoryTree.FileSelected) -> None:
        self.query_one("#path", Input).value = str(event.path)

    @on(Input.Submitted)
    @on(Button.Pressed, "#open")
    def action_open(self) -> None:
        path = Path(self.query_one("#path", Input).value.strip()).expanduser()
        if not path.exists():
            self.query_one("#error", Static).update(Text(f"no such file or directory: {path}", style="bold red"))
            return
        self.dismiss(str(path.resolve()))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ExportDialog(ModalScreen[str | None]):
    """Save the current output to a file; ``writer(path, fmt, with_stacks)`` does the writing."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title: str, formats: list[str], default_name: str, writer: Callable[[str, str, bool], None]):
        super().__init__()
        self.title_text = title
        self.formats = formats
        self.default_name = default_name
        self.writer = writer

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self.title_text, style="bold"))
            yield Label("Format")
            yield Select([(f, f) for f in self.formats], value=self.formats[0], allow_blank=False, id="format")
            yield Label("File")
            yield Input(self._default_path(self.formats[0]), id="path")
            if "text" in self.formats:
                yield Checkbox("Include example stacks (text)", True, id="stacks")
            yield Static(id="error")
            with Horizontal(id="buttons"):
                yield Button("Export", variant="primary", id="export")
                yield Button("Cancel", id="cancel")

    def _default_path(self, fmt: str) -> str:
        ext = {"text": "txt", "json": "json", "html": "html"}[fmt]
        return str(Path.cwd() / f"{self.default_name}.{ext}")

    def on_mount(self) -> None:
        self.query_one("#path", Input).focus()

    @on(Select.Changed, "#format")
    def _format_changed(self, event: Select.Changed) -> None:
        path = self.query_one("#path", Input)
        if Path(path.value).stem == self.default_name:
            path.value = self._default_path(str(event.value))

    @on(Input.Changed, "#path")
    def _path_changed(self, event: Input.Changed) -> None:
        """Typing "report.json" selects the JSON format (and so on)."""
        fmt = {".txt": "text", ".json": "json", ".html": "html", ".htm": "html"}.get(Path(event.value).suffix.lower())
        select = self.query_one("#format", Select)
        if fmt in self.formats and select.value != fmt:
            select.value = fmt

    @on(Input.Submitted)
    @on(Button.Pressed, "#export")
    def action_export(self) -> None:
        path = Path(self.query_one("#path", Input).value.strip()).expanduser()
        fmt = str(self.query_one("#format", Select).value)
        stacks = self.query("#stacks")
        with_stacks = bool(stacks.first(Checkbox).value) if stacks else False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.writer(str(path), fmt, with_stacks)
        except OSError as exc:
            self.query_one("#error", Static).update(Text(str(exc), style="bold red"))
            return
        self.dismiss(str(path))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)
