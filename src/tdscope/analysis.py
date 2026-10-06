"""Analyses over a chronologically ordered list of thread dumps.

Every function takes the dumps plus a :class:`ThreadFilter` and returns plain
dataclasses, so the results can be rendered as text or serialized to JSON.

Cross-dump analyses (``stuck``, ``cpu``, request spans) compare dumps of the same
JVM only (see :attr:`ThreadDump.jvm_id`), so dumps of several nodes can be mixed.
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from statistics import mean

from .filters import ThreadFilter
from .model import ThreadDump, ThreadInfo

# Top frames of threads that are RUNNABLE from the JVM's point of view but really idle,
# blocked in native code waiting for work (selectors, accept loops, reference handler).
IDLE_NATIVE_FRAMES = (
    "java.lang.ref.Reference.waitForReferencePendingList",
    "sun.nio.ch.EPoll.wait",
    "sun.nio.ch.EPoll.epollWait",
    "sun.nio.ch.EPollArrayWrapper.epollWait",
    "sun.nio.ch.KQueue.poll",
    "sun.nio.ch.KQueue.keventPoll",
    "sun.nio.ch.KQueueArrayWrapper.kevent0",
    "sun.nio.ch.WEPoll.wait",
    "sun.nio.ch.WindowsSelectorImpl$SubSelector.poll0",
    "sun.nio.ch.Net.accept",
    "sun.nio.ch.ServerSocketChannelImpl.accept0",
    "java.net.PlainSocketImpl.socketAccept",
    "java.net.PlainSocketImpl.accept0",
    "java.lang.ProcessHandleImpl.waitForProcessExit0",
    "java.lang.UNIXProcess.waitForProcessExit",
)


# Top frames of a blocking network read: RUNNABLE while waiting for the remote side.
# Normal for background clients (HTTP/2 connection readers, long polling).
NETWORK_WAIT_FRAMES = (
    "java.net.SocketInputStream.socketRead0",
    "sun.nio.ch.SocketDispatcher.read0",
    "sun.nio.ch.Net.poll",
)


def _top_method(thread: ThreadInfo) -> str | None:
    return thread.frames[0].split("(", 1)[0] if thread.frames else None


def is_idle_native(thread: ThreadInfo) -> bool:
    return _top_method(thread) in IDLE_NATIVE_FRAMES


def is_network_wait(thread: ThreadInfo) -> bool:
    return _top_method(thread) in NETWORK_WAIT_FRAMES


# Lock kinds meaning "I want this lock and cannot proceed" (as opposed to Object.wait()).
_CONTENDED_KINDS = ("waiting to lock", "parking to wait for")


def _selected(dump: ThreadDump, flt: ThreadFilter) -> list[ThreadInfo]:
    return [t for t in dump.threads if flt.matches(t)]


def _thread_key(dump: ThreadDump, thread: ThreadInfo) -> tuple[str, str]:
    """Identity of a thread across dumps: (JVM, native thread address or name)."""
    return (dump.jvm_id, thread.tid or thread.name)


def by_jvm(dumps: Sequence[ThreadDump]) -> list[list[ThreadDump]]:
    """Split chronologically ordered dumps into one series per JVM process."""
    series: dict[str, list[ThreadDump]] = {}
    for dump in dumps:
        series.setdefault(dump.jvm_id, []).append(dump)
    return list(series.values())


def _seconds_between(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


@dataclass
class Stats:
    count: int
    min: float
    avg: float
    max: float

    @classmethod
    def of(cls, values: Sequence[float]) -> Stats | None:
        if not values:
            return None
        return cls(count=len(values), min=min(values), avg=mean(values), max=max(values))


# --------------------------------------------------------------------------- summary


@dataclass
class DumpSummary:
    label: str
    timestamp: datetime | None
    vm_info: str | None
    threads: int
    selected: int
    states: dict[str, int]
    http_requests: int
    deadlocks: list[list[str]]


def summary(dumps: Sequence[ThreadDump], flt: ThreadFilter) -> list[DumpSummary]:
    result = []
    for dump in dumps:
        selected = _selected(dump, flt)
        states = Counter(t.state or "(no state)" for t in selected)
        result.append(
            DumpSummary(
                label=dump.label,
                timestamp=dump.timestamp,
                vm_info=dump.vm_info,
                threads=len(dump.threads),
                selected=len(selected),
                states=dict(states.most_common()),
                http_requests=sum(1 for t in selected if t.request),
                deadlocks=[d.threads for d in dump.deadlocks],
            )
        )
    return result


# -------------------------------------------------------------------------- requests


@dataclass
class RequestGroup:
    key: str
    snapshots: int  # thread occurrences across all dumps
    distinct_requests: int
    dumps_seen: int
    request_age_s: Stats | None  # dump time - request start time (needs [epoch-ms] in thread name)
    request_age_invalid: int  # snapshots where the request started "after" the dump: wrong --tz?
    observed_span_s: float | None  # longest time a single request was seen across dumps
    thread_age_s: Stats | None  # header "elapsed=": age of the *thread*, not of the request
    cpu_ms: Stats | None
    states: dict[str, int]
    top_frames: dict[str, int]  # first frame matching --match (or top of stack)
    example_thread: str
    example_stack: str


def _strip_query(path: str) -> str:
    return path.split("?", 1)[0]


def requests(
    dumps: Sequence[ThreadDump],
    flt: ThreadFilter,
    keep_query: bool = False,
    tz: tzinfo | None = None,
) -> list[RequestGroup]:
    """Group HTTP request threads by ``METHOD path``.

    ``tz`` is the timezone of the JVM that wrote the dumps (dump timestamps are
    local time without zone); ``None`` means the timezone of this machine.
    """
    snapshots: dict[str, list[tuple[ThreadDump, ThreadInfo]]] = defaultdict(list)
    for dump in dumps:
        for thread in _selected(dump, flt):
            if thread.request is None:
                continue
            req = thread.request
            path = req.path if keep_query else _strip_query(req.path)
            snapshots[f"{req.method} {path}"].append((dump, thread))

    groups = []
    for key, items in snapshots.items():
        ages: list[float] = []
        invalid_ages = 0
        seen_per_request: dict[tuple[tuple[str, str], str], list[datetime]] = defaultdict(list)
        for dump, thread in items:
            assert thread.request is not None
            started = thread.request.started_at_ms
            if started is not None and dump.timestamp is not None:
                ts = dump.timestamp.replace(tzinfo=tz) if tz else dump.timestamp
                age = ts.timestamp() - started / 1000.0
                if age >= -1:  # dump timestamps have a 1 s resolution
                    ages.append(max(age, 0.0))
                else:
                    invalid_ages += 1
            if dump.timestamp is not None:
                seen_per_request[(_thread_key(dump, thread), thread.name)].append(dump.timestamp)

        spans = [(max(ts) - min(ts)).total_seconds() for ts in seen_per_request.values() if len(ts) > 1]
        top_frames = Counter(
            (flt.first_matching_frame(t) if flt.has_frame_patterns else (t.frames[0] if t.frames else None))
            or "(no frame)"
            for _, t in items
        )
        example = max(items, key=lambda it: len(it[1].frames))[1]
        groups.append(
            RequestGroup(
                key=key,
                snapshots=len(items),
                distinct_requests=len({(_thread_key(d, t), t.name) for d, t in items}),
                dumps_seen=len({id(d) for d, _ in items}),
                request_age_s=Stats.of(ages),
                request_age_invalid=invalid_ages,
                observed_span_s=max(spans) if spans else None,
                thread_age_s=Stats.of([t.elapsed_s for _, t in items if t.elapsed_s is not None]),
                cpu_ms=Stats.of([t.cpu_ms for _, t in items if t.cpu_ms is not None]),
                states=dict(Counter(t.state or "(no state)" for _, t in items).most_common()),
                top_frames=dict(top_frames.most_common(5)),
                example_thread=example.header,
                example_stack=example.stack_text,
            )
        )
    groups.sort(key=lambda g: (-g.snapshots, g.key))
    return groups


# ---------------------------------------------------------------------------- frames


@dataclass
class FrameGroup:
    frame: str
    snapshots: int
    distinct_threads: int
    example_thread: str
    example_stack: str


def frames(dumps: Sequence[ThreadDump], flt: ThreadFilter, top_only: bool = False) -> list[FrameGroup]:
    """Unique stack frames matching ``flt.frame_patterns`` with occurrence counts.

    ``top_only`` counts only the top-most matching frame of each thread.
    """
    if not flt.has_frame_patterns:
        raise ValueError("the frames analysis needs at least one --match pattern")
    occurrences: dict[str, list[tuple[ThreadDump, ThreadInfo]]] = defaultdict(list)
    for dump in dumps:
        for thread in _selected(dump, flt):
            matching = [f for f in thread.frames if flt.frame_matches(f)]
            if top_only:
                matching = matching[:1]
            for frame in dict.fromkeys(matching):
                occurrences[frame].append((dump, thread))

    result = [
        FrameGroup(
            frame=frame,
            snapshots=len(items),
            distinct_threads=len({_thread_key(d, t) for d, t in items}),
            example_thread=items[0][1].header,
            example_stack=items[0][1].stack_text,
        )
        for frame, items in occurrences.items()
    ]
    result.sort(key=lambda g: (-g.snapshots, g.frame))
    return result


# -------------------------------------------------------------------------- hotspots


@dataclass
class StackGroup:
    signature: list[str]
    snapshots: int
    distinct_threads: int
    states: dict[str, int]
    thread_names: list[str]
    example_stack: str


def hotspots(dumps: Sequence[ThreadDump], flt: ThreadFilter, depth: int = 10) -> list[StackGroup]:
    """Group threads by identical top ``depth`` frames (most frequent first)."""
    groups: dict[tuple[str, ...], list[tuple[ThreadDump, ThreadInfo]]] = defaultdict(list)
    for dump in dumps:
        for thread in _selected(dump, flt):
            if thread.frames:
                groups[tuple(thread.frames[:depth])].append((dump, thread))

    result = [
        StackGroup(
            signature=list(sig),
            snapshots=len(items),
            distinct_threads=len({_thread_key(d, t) for d, t in items}),
            states=dict(Counter(t.state or "(no state)" for _, t in items).most_common()),
            thread_names=list(dict.fromkeys(t.name for _, t in items))[:5],
            example_stack=items[0][1].stack_text,
        )
        for sig, items in groups.items()
    ]
    result.sort(key=lambda g: (-g.snapshots, g.signature))
    return result


# ----------------------------------------------------------------------------- stuck


def _stable_runs(
    series: Sequence[ThreadDump], flt: ThreadFilter, depth: int, interesting: Callable[[ThreadInfo], bool]
) -> list[tuple[int, int, ThreadInfo]]:
    """Maximal runs of consecutive dumps in which a thread keeps the same stack.

    Returns ``(first dump index, run length, thread as seen in the first dump)``.
    """
    Run = tuple[tuple[str, ...], int, int, ThreadInfo]  # (signature, start, length, thread)
    runs: dict[tuple[str, str], Run] = {}
    finished: list[tuple[int, int, ThreadInfo]] = []
    for i, dump in enumerate(series):
        current: dict[tuple[str, str], Run] = {}
        for thread in _selected(dump, flt):
            if not interesting(thread):
                continue
            key = (thread.tid or "", thread.name)
            sig = tuple(thread.frames[:depth])
            prev = runs.pop(key, None)
            if prev and prev[0] == sig:
                current[key] = (sig, prev[1], prev[2] + 1, prev[3])
            else:
                if prev:
                    finished.append(prev[1:])
                current[key] = (sig, i, 1, thread)
        finished.extend(run[1:] for run in runs.values())  # threads gone from this dump
        runs = current
    finished.extend(run[1:] for run in runs.values())
    return finished


@dataclass
class StuckThread:
    name: str
    tid: str | None
    state: str | None
    consecutive_dumps: int
    span_s: float | None
    first_seen: str
    last_seen: str
    request: str | None
    example_stack: str


def stuck(
    dumps: Sequence[ThreadDump],
    flt: ThreadFilter,
    min_dumps: int = 3,
    depth: int = 20,
    all_states: bool = False,
    include_network_wait: bool = False,
) -> list[StuckThread]:
    """Threads that keep the same stack over ``min_dumps`` consecutive dumps.

    By default only RUNNABLE/BLOCKED threads and threads serving an HTTP request are
    considered, because idle pool workers legitimately keep an identical stack. Threads
    "running" in a known idle native frame (see :data:`IDLE_NATIVE_FRAMES`) are skipped too,
    and so are background threads blocked in a network read unless ``include_network_wait``.
    """

    def interesting(t: ThreadInfo) -> bool:
        if not t.frames:
            return False
        if all_states or t.request is not None:
            return True
        if is_idle_native(t) or (is_network_wait(t) and not include_network_wait):
            return False
        return t.state in ("RUNNABLE", "BLOCKED")

    result = []
    for series in by_jvm(dumps):
        for start, length, thread in _stable_runs(series, flt, depth, interesting):
            if length < min_dumps:
                continue
            first, last = series[start], series[start + length - 1]
            result.append(
                StuckThread(
                    name=thread.name,
                    tid=thread.tid,
                    state=thread.state,
                    consecutive_dumps=length,
                    span_s=_seconds_between(first.timestamp, last.timestamp),
                    first_seen=first.label,
                    last_seen=last.label,
                    request=thread.request.key if thread.request else None,
                    example_stack=thread.stack_text,
                )
            )
    result.sort(key=lambda s: (-s.consecutive_dumps, -(s.span_s or 0), s.name))
    return result


# ----------------------------------------------------------------------------- locks


@dataclass
class LockContention:
    dump: str
    address: str
    class_name: str | None
    owner: str | None
    owner_state: str | None
    owner_frame: str | None
    waiters: list[str]


@dataclass
class LockReport:
    contentions: list[LockContention] = field(default_factory=list)
    deadlocks: list[tuple[str, list[str]]] = field(default_factory=list)  # (dump label, thread names)


def locks(dumps: Sequence[ThreadDump], flt: ThreadFilter) -> LockReport:
    """Monitors / j.u.c. locks that other threads are blocked on, plus JVM-detected deadlocks.

    The filter selects the *waiting* threads; owners are looked up among all threads.
    """
    report = LockReport()
    for dump in dumps:
        owners: dict[str, ThreadInfo] = {}
        for thread in dump.threads:
            for ev in thread.held_locks():
                if ev.address:
                    owners.setdefault(ev.address, thread)

        waiters: dict[str, list[ThreadInfo]] = defaultdict(list)
        classes: dict[str, str | None] = {}
        for thread in _selected(dump, flt):
            wanted = thread.waiting_for()
            if wanted is None or wanted.address is None or wanted.kind not in _CONTENDED_KINDS:
                continue
            owner = owners.get(wanted.address)
            # Parking on a condition nobody owns is an idle worker, not contention.
            if wanted.kind == "parking to wait for" and owner is None:
                continue
            if owner is thread:
                continue
            waiters[wanted.address].append(thread)
            classes[wanted.address] = wanted.class_name

        for address, waiting in waiters.items():
            owner = owners.get(address)
            report.contentions.append(
                LockContention(
                    dump=dump.label,
                    address=address,
                    class_name=classes[address],
                    owner=owner.name if owner else None,
                    owner_state=owner.state if owner else None,
                    owner_frame=owner.frames[0] if owner and owner.frames else None,
                    waiters=[t.name for t in waiting],
                )
            )
        report.deadlocks.extend((dump.label, d.threads) for d in dump.deadlocks)

    report.contentions.sort(key=lambda c: (-len(c.waiters), c.dump, c.address))
    return report


# ------------------------------------------------------------------------------- cpu


@dataclass
class CpuConsumer:
    name: str
    tid: str | None
    cpu_ms: float
    wall_pct: float | None  # cpu time / wall time between the two dumps, 100 = one full core
    state: str | None
    top_frame: str | None


@dataclass
class CpuInterval:
    start: str
    end: str
    interval_s: float | None
    top: list[CpuConsumer]


def cpu(dumps: Sequence[ThreadDump], flt: ThreadFilter, top: int = 10) -> list[CpuInterval]:
    """Threads that burned the most CPU between consecutive dumps (needs JDK 11+ ``cpu=``)."""
    result = []
    pairs = [pair for series in by_jvm(dumps) for pair in itertools.pairwise(series)]
    for prev, curr in pairs:
        before = {(t.tid, t.nid): t.cpu_ms for t in prev.threads if t.cpu_ms is not None}
        interval = _seconds_between(prev.timestamp, curr.timestamp)
        consumers = []
        for thread in _selected(curr, flt):
            if thread.cpu_ms is None:
                continue
            # A thread absent from the previous dump was started in between: all its CPU counts.
            delta = thread.cpu_ms - before.get((thread.tid, thread.nid), 0.0)
            if delta <= 0:
                continue
            consumers.append(
                CpuConsumer(
                    name=thread.name,
                    tid=thread.tid,
                    cpu_ms=round(delta, 2),
                    wall_pct=round(delta / (interval * 10), 1) if interval else None,
                    state=thread.state,
                    top_frame=thread.frames[0] if thread.frames else None,
                )
            )
        consumers.sort(key=lambda c: -c.cpu_ms)
        result.append(CpuInterval(start=prev.label, end=curr.label, interval_s=interval, top=consumers[:top]))
    return result
