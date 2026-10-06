"""Dynamic robustness tests: hostile or random input must never crash or hang tdscope.

Run deeper with ``HYPOTHESIS_PROFILE=dast pytest -m dast`` (as the security workflow does).
"""

import time
from datetime import timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tdscope import ThreadFilter
from tdscope import analysis as a
from tdscope.cli import main
from tdscope.parser import parse_lines
from tdscope.timeline import GROUPINGS, MODES, build_chart, to_html
from tdscope.views import to_table

pytestmark = pytest.mark.dast

settings.register_profile("default", max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("dast", max_examples=1500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(__import__("os").environ.get("HYPOTHESIS_PROFILE", "default"))

TOKEN = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40)
NAME = st.one_of(
    TOKEN,
    st.sampled_from(["qtp1-2", "main", "pool-1-thread-3", '"quoted"', "</script>", "a\tb"]),
    st.builds(
        lambda ip, ms, m, p: f"{ip} [{ms}] {m} {p} HTTP/1.1",
        TOKEN,
        st.integers(0, 10**13),
        st.sampled_from(["GET", "POST", "DELETE"]),
        TOKEN,
    ),
)
HEADER = st.builds(
    lambda name, n, attrs, status: f'"{name}" #{n} {attrs} {status}',
    NAME,
    st.integers(-5, 10**6),
    st.sampled_from(
        [
            "prio=5 os_prio=0 tid=0x1 nid=0x2",
            "daemon prio=5 os_prio=-1 cpu=1.5ms elapsed=2.25s tid=0x00007f nid=12",
            "[12] prio=x os_prio= cpu=ms elapsed=s tid= nid=0xzz",
            "prio=5",
        ]
    ),
    TOKEN,
)
LINE = st.one_of(
    HEADER,
    TOKEN,
    st.builds(lambda t: f"\tat {t}", TOKEN),
    st.builds(
        lambda k, a_, c: f"\t- {k} <{a_}> (a {c})",
        st.sampled_from(["locked", "waiting to lock", "parking to wait for ", "waiting on", "x y z"]),
        TOKEN,
        TOKEN,
    ),
    st.builds(lambda s_: f"   java.lang.Thread.State: {s_}", TOKEN),
    st.sampled_from(
        [
            "2026-01-01 10:00:00",
            "Full thread dump OpenJDK (x):",
            "JNI global refs: 1, weak refs: 0",
            "   Locked ownable synchronizers:",
            "\t- None",
            "\t- <0x1> (a java.util.concurrent.locks.ReentrantLock$Sync)",
            "Found one Java-level deadlock:",
            '"t":',
            "Found 1 deadlock.",
            "",
            "Threads class SMR info:",
            "}",
        ]
    ),
)


def _run_everything(dumps):
    flt = ThreadFilter()
    results = {
        "summary": a.summary(dumps, flt),
        "requests": a.requests(dumps, flt, tz=timezone.utc),
        "frames": a.frames(dumps, ThreadFilter(frame_patterns=["a"])),
        "hotspots": a.hotspots(dumps, flt),
        "stuck": a.stuck(dumps, flt, min_dumps=1, all_states=True, include_network_wait=True),
        "locks": a.locks(dumps, flt),
        "cpu": a.cpu(dumps, flt),
    }
    for command, result in results.items():
        to_table(command, result)
    for mode in MODES:
        for by in GROUPINGS:
            to_html(build_chart(dumps, mode=mode, group_by=by, max_groups=3))


@given(st.lists(st.text(), max_size=40))
def test_arbitrary_text_never_crashes(lines):
    _run_everything(list(parse_lines(lines)))


@given(st.lists(LINE, max_size=80))
def test_dump_shaped_garbage_never_crashes(lines):
    dumps = list(parse_lines(lines))
    for dump in dumps:
        for thread in dump.threads:
            assert isinstance(thread.name, str)
            assert all(isinstance(f, str) for f in thread.frames)
    _run_everything(dumps)


# Lines shaped to trigger catastrophic regex backtracking. Each must be parsed in linear
# time; the lock line used to take minutes before the lock regex was fixed.
N = 200_000
HOSTILE = {
    "lock kind with spaces": "\t- a" + " " * N + "x",
    "lock kind words": "\t- " + "a " * N + "x",
    "lock kind parens": "\t- a" + " (" * N,
    "synchronizer": "\t- <" + "a" * N + "> (a " + "b" * N,
    "header quotes": '"' + '" ' * N + "x",
    "header tabs": '"' + '"\t' * N,
    "http method repeated": '"' + "GET " * N + '" #1 prio=5 tid=0x1 nid=0x1 runnable',
    "http path spaces": '"GET /' + " " * N + 'x" #1 prio=5 tid=0x1 nid=0x1 runnable',
    "state": "   java.lang.Thread.State: A" + " " * N + "x",
    "deadlock name": '"' + '":' * N + "x",
    "frame": "\tat " + "a." * N + "m(" + "(" * N,
}


@pytest.mark.parametrize("line", HOSTILE.values(), ids=HOSTILE.keys())
def test_hostile_lines_are_parsed_in_linear_time(line):
    lines = [
        "Full thread dump X:",
        '"t" #1 prio=5 os_prio=0 tid=0x1 nid=0x1 runnable',
        line,
        "Found one Java-level deadlock:",
        line,
        "Found 1 deadlock.",
    ]
    start = time.perf_counter()
    dumps = list(parse_lines(lines))
    _run_everything(dumps)
    assert time.perf_counter() - start < 3.0  # ~0.1 s when linear, minutes when quadratic


def test_cli_on_binary_garbage(tmp_path):
    junk = tmp_path / "junk.dump"
    junk.write_bytes(bytes(range(256)) * 400)
    assert main(["summary", str(junk), "-q"]) == 1  # "no thread dumps found", no traceback
