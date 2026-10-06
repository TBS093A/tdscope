"""Command line interface: ``tdscope <analysis> [options] PATH...``."""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
from collections.abc import Callable, Sequence
from typing import Any

from . import __version__, report
from . import analysis as a
from .filters import ThreadFilter
from .loader import DEFAULT_PATTERNS, load
from .model import ThreadDump

Analysis = Callable[[Sequence[ThreadDump], ThreadFilter, argparse.Namespace], Any]


def _tz(value: str) -> Any:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"unknown time zone: {value}") from exc


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


# name -> (help, analysis, renderer)
COMMANDS: dict[str, tuple[str, Analysis, Callable[..., None]]] = {
    "summary": (
        "per-dump overview: thread counts, states, HTTP requests, deadlocks",
        lambda d, f, o: a.summary(d, f),
        report.render_summary,
    ),
    "requests": (
        "HTTP request threads grouped by method and path, with request age",
        lambda d, f, o: a.requests(d, f, keep_query=o.keep_query, tz=o.tz),
        report.render_requests,
    ),
    "frames": (
        "unique stack frames matching --match, with counts",
        lambda d, f, o: a.frames(d, f, top_only=o.top_only),
        report.render_frames,
    ),
    "hotspots": (
        "threads grouped by identical stack (most common first)",
        lambda d, f, o: a.hotspots(d, f, depth=o.depth),
        report.render_hotspots,
    ),
    "stuck": (
        "threads with the same stack across consecutive dumps",
        lambda d, f, o: a.stuck(d, f, min_dumps=o.min_dumps, depth=o.depth, all_states=o.all_states),
        report.render_stuck,
    ),
    "locks": (
        "lock contention (blocked threads and lock owners) and deadlocks",
        lambda d, f, o: a.locks(d, f),
        report.render_locks,
    ),
    "cpu": (
        "top CPU consumers between consecutive dumps (JDK 11+)",
        lambda d, f, o: a.cpu(d, f, top=o.top or 10),
        report.render_cpu,
    ),
}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("paths", nargs="+", metavar="PATH", help="dump files or directories (recursive); '-' for stdin")
    sel = common.add_argument_group("thread selection")
    sel.add_argument(
        "-m",
        "--match",
        action="append",
        default=[],
        metavar="PATTERN",
        help="keep threads with a frame containing PATTERN, e.g. 'io.wcm.' (repeatable)",
    )
    sel.add_argument("-E", "--regex", action="store_true", help="treat --match patterns as regular expressions")
    sel.add_argument(
        "-s",
        "--state",
        action="append",
        default=[],
        metavar="STATE",
        help="keep threads in this java.lang.Thread.State, e.g. BLOCKED (repeatable)",
    )
    sel.add_argument("-n", "--name", metavar="REGEX", help="keep threads whose name matches REGEX")
    sel.add_argument("--http-only", action="store_true", help="keep only threads serving an HTTP request")
    inp = common.add_argument_group("input")
    inp.add_argument(
        "-g",
        "--glob",
        action="append",
        metavar="GLOB",
        help=f"file name patterns used when scanning directories (default: {' '.join(DEFAULT_PATTERNS)})",
    )
    outp = common.add_argument_group("output")
    outp.add_argument("-f", "--format", choices=("text", "json"), default="text")
    outp.add_argument("-o", "--output", metavar="FILE", help="write the report to FILE instead of stdout")
    outp.add_argument("-t", "--top", type=_positive_int, metavar="N", help="show only the first N results")
    outp.add_argument("--stack", action="store_true", help="include an example stack trace for each result")
    outp.add_argument("--max-stack-lines", type=_positive_int, metavar="N", help="truncate printed stacks to N lines")
    outp.add_argument("-q", "--quiet", action="store_true", help="no progress information on stderr")

    parser = argparse.ArgumentParser(
        prog="tdscope",
        description="Analyze Java (HotSpot) thread dumps: requests, hot stacks, stuck threads, locks and CPU.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="ANALYSIS")
    subparsers = {name: sub.add_parser(name, parents=[common], help=help_) for name, (help_, _, _) in COMMANDS.items()}

    subparsers["requests"].add_argument(
        "--keep-query", action="store_true", help="do not strip the query string when grouping paths"
    )
    subparsers["requests"].add_argument(
        "--tz",
        type=_tz,
        metavar="ZONE",
        help="time zone of the JVM that wrote the dumps, e.g. Europe/Warsaw (default: local time zone)",
    )
    subparsers["frames"].add_argument(
        "--top-only", action="store_true", help="count only the top-most matching frame of each thread"
    )
    for name, default in (("hotspots", 10), ("stuck", 20)):
        subparsers[name].add_argument(
            "--depth", type=_positive_int, default=default, help=f"stack frames compared (default: {default})"
        )
    subparsers["stuck"].add_argument(
        "--min-dumps", type=_positive_int, default=3, help="minimum consecutive dumps (default: 3)"
    )
    subparsers["stuck"].add_argument(
        "--all-states", action="store_true", help="also consider idle threads (WAITING / TIMED_WAITING)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    opts = parser.parse_args(argv)
    _, run, render = COMMANDS[opts.command]

    if opts.command == "frames" and not opts.match:
        parser.error("frames: at least one --match PATTERN is required")

    try:
        flt = ThreadFilter(
            frame_patterns=opts.match,
            regex=opts.regex,
            states=opts.state,
            name_pattern=opts.name,
            http_only=opts.http_only,
        )
    except re.error as exc:
        parser.error(f"invalid pattern: {exc}")

    try:
        dumps = load(opts.paths, patterns=opts.glob or DEFAULT_PATTERNS)
    except FileNotFoundError as exc:
        print(f"tdscope: {exc}", file=sys.stderr)
        return 2
    if not dumps:
        print("tdscope: no thread dumps found", file=sys.stderr)
        return 1
    if not opts.quiet:
        sources = len({d.source for d in dumps})
        print(f"tdscope: {len(dumps)} dump(s) from {sources} file(s)", file=sys.stderr)

    result = run(dumps, flt, opts)
    if opts.top and isinstance(result, list) and opts.command != "cpu":
        result = result[: opts.top]

    with open(opts.output, "w", encoding="utf-8") if opts.output else contextlib.nullcontext(sys.stdout) as out:
        if opts.format == "json":
            report.to_json(result, out)
        else:
            render(result, out, show_stack=opts.stack, max_lines=opts.max_stack_lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
