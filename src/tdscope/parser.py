"""Streaming parser for HotSpot thread dumps (jstack, jcmd Thread.print, kill -3).

Supported formats: JDK 8 through JDK 21+ (``cpu=``/``elapsed=`` attributes, the
JDK 19+ ``#1 [12345]`` header with a decimal ``nid``). Several dumps in one file are
supported, including dumps embedded in an application log (``kill -3`` output).

The parser is line based: it does not rely on blank lines separating threads, so
"Locked ownable synchronizers" blocks, CRLF line endings and the JDK 17+
"Threads class SMR info" block are handled correctly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import datetime

from .model import Deadlock, HttpRequest, LockEvent, ThreadDump, ThreadInfo

_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$")
_DUMP_START_RE = re.compile(r"^Full thread dump (?P<vm>.*?):?\s*$")
# A thread header: quoted name followed by at least one known attribute.
_THREAD_HEADER_RE = re.compile(
    r'^"(?P<name>.*)"\s+(?P<rest>(?:#\d+|\[\d+\]|daemon|prio=|os_prio=|cpu=|elapsed=|tid=|nid=).*)$'
)
_STATE_RE = re.compile(r"^java\.lang\.Thread\.State:\s*(?P<state>[A-Z_]+)(?:\s*\((?P<detail>.*)\))?")
_LOCK_RE = re.compile(r"^-\s+(?P<kind>[a-z][a-z \-()]*?)\s+<(?P<addr>[^>]*)>\s*(?:\(a (?P<cls>.+)\))?\s*$")
_SYNC_RE = re.compile(r"^-\s+<(?P<addr>[^>]*)>\s*(?:\(a (?P<cls>.+)\))?\s*$")
_DEADLOCK_START_RE = re.compile(r"^Found one Java-level deadlock:")
_DEADLOCK_END_RE = re.compile(r"^Found \d+ deadlocks?\.")
_DEADLOCK_THREAD_RE = re.compile(r'^"(?P<name>.+)":\s*$')

_HTTP_RE = re.compile(
    r"\b(?P<method>GET|POST|PUT|DELETE|HEAD|PATCH|OPTIONS|TRACE|CONNECT|PROPFIND|PROPPATCH|MKCOL|COPY|MOVE|LOCK|UNLOCK)"
    r"\s+(?P<path>\S+)\s+(?P<proto>HTTP/\d(?:\.\d)?)"
)
_REQUEST_START_RE = re.compile(r"\[(\d{12,13})\]")


def parse_request(thread_name: str) -> HttpRequest | None:
    """Extract HTTP request info that servlet containers put into thread names."""
    m = _HTTP_RE.search(thread_name)
    if not m:
        return None
    started = _REQUEST_START_RE.search(thread_name)
    return HttpRequest(
        method=m.group("method"),
        path=m.group("path"),
        protocol=m.group("proto"),
        started_at_ms=int(started.group(1)) if started else None,
    )


def _parse_int(value: str) -> int | None:
    try:
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    except ValueError:
        return None


def _parse_float(value: str, suffix: str) -> float | None:
    try:
        return float(value[: -len(suffix)] if value.endswith(suffix) else value)
    except ValueError:
        return None


def parse_thread_header(line: str) -> ThreadInfo | None:
    m = _THREAD_HEADER_RE.match(line)
    if not m:
        return None
    name, rest = m.group("name"), m.group("rest")
    thread = ThreadInfo(name=name, header=line)

    # Plain token handling instead of regexes: headers are parsed once per thread per dump.
    tokens = rest.split()
    status_start = 0
    for i, token in enumerate(tokens):
        if token.startswith("#") and i == 0:
            thread.number = _parse_int(token[1:])
        elif token == "daemon":
            thread.daemon = True
        elif "=" in token:
            key, value = token.split("=", 1)
            if key == "prio":
                thread.prio = _parse_int(value)
            elif key == "os_prio":
                thread.os_prio = _parse_int(value)
            elif key == "cpu":
                thread.cpu_ms = _parse_float(value, "ms")
            elif key == "elapsed":
                thread.elapsed_s = _parse_float(value, "s")
            elif key == "tid":
                thread.tid = value
            elif key == "nid":
                thread.nid = _parse_int(value)
        elif not (token.startswith("[") and not token.startswith("[0x")):  # JDK 19+ "[12345]"
            continue
        status_start = i + 1
    status = tokens[status_start:]
    if status and status[-1].startswith("[0x"):
        status.pop()
    thread.status = " ".join(status)
    if "HTTP/" in name:
        thread.request = parse_request(name)
    return thread


def _parse_timestamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


class _DumpBuilder:
    """Mutable parsing state for a single dump."""

    def __init__(self, source: str, index: int, timestamp: datetime | None, vm_info: str | None):
        self.dump = ThreadDump(source=source, index=index, timestamp=timestamp, vm_info=vm_info)
        self.thread: ThreadInfo | None = None
        self.in_synchronizers = False
        self.deadlock_lines: list[str] | None = None
        self.ended = False  # past "JNI global refs": only a deadlock report may follow

    def feed(self, line: str, stripped: str) -> None:
        # Cheap prefix checks first: this runs for every line of potentially huge inputs.
        thread = self.thread
        if thread is not None and stripped.startswith("at ") and self.deadlock_lines is None:
            thread.frames.append(stripped[3:])
            return

        if self.deadlock_lines is not None:
            if stripped.startswith("Found one Java-level deadlock"):
                self._close_deadlock()
                self.deadlock_lines = [stripped]
            elif stripped.startswith("Found ") and _DEADLOCK_END_RE.match(stripped):
                self._close_deadlock()
            else:
                self.deadlock_lines.append(line)
            return

        if not stripped:
            return
        first = stripped[0]

        if first == '"':
            header = parse_thread_header(stripped)
            if header is not None:
                self._close_thread()
                self.thread = header
            return
        if first == "F" and _DEADLOCK_START_RE.match(stripped):
            self._close_thread()
            self.deadlock_lines = [stripped]
            return
        if thread is None:
            return

        if first == "-":
            if self.in_synchronizers:
                sync = _SYNC_RE.match(stripped)
                if sync:
                    thread.owned_synchronizers.append(LockEvent("owns", sync.group("addr"), sync.group("cls")))
                return
            lock = _LOCK_RE.match(stripped)
            if lock:
                addr = lock.group("addr")
                event = LockEvent(
                    kind=lock.group("kind").strip(),
                    address=addr if addr.startswith("0x") else None,
                    class_name=lock.group("cls"),
                )
                thread.locks.append((max(len(thread.frames) - 1, 0), event))
            return
        if first == "j":
            state = _STATE_RE.match(stripped)
            if state:
                thread.state = state.group("state")
                thread.state_detail = state.group("detail")
            return
        if stripped == "Locked ownable synchronizers:":
            self.in_synchronizers = True

    def _close_thread(self) -> None:
        if self.thread is not None:
            self.dump.threads.append(self.thread)
        self.thread = None
        self.in_synchronizers = False

    def _close_deadlock(self) -> None:
        lines = self.deadlock_lines or []
        self.deadlock_lines = None
        names: list[str] = []
        for raw in lines:
            if raw.strip().startswith("Java stack information"):
                break
            m = _DEADLOCK_THREAD_RE.match(raw.strip())
            if m and m.group("name") not in names:
                names.append(m.group("name"))
        if names:
            self.dump.deadlocks.append(Deadlock(threads=names, text="\n".join(lines)))

    def finish(self) -> ThreadDump:
        if self.deadlock_lines is not None:
            self._close_deadlock()
        self._close_thread()
        return self.dump


def parse_lines(lines: Iterable[str], source: str = "<input>") -> Iterator[ThreadDump]:
    """Parse every thread dump found in ``lines``.

    Text outside of dumps (application logs, heap summaries, ...) is ignored. If the
    input contains thread blocks but no "Full thread dump" banner (e.g. a fragment
    copied from a console), they are returned as a single dump without timestamp.
    """
    builder: _DumpBuilder | None = None
    index = 0
    last_timestamp: datetime | None = None

    for raw in lines:
        line = raw.rstrip("\r\n")
        stripped = line.strip()

        # Fast path for the bulk of the input: stack frames and lock lines of a thread.
        if builder is not None and not builder.ended and stripped[:1] in ("a", "-"):
            builder.feed(line, stripped)
            continue

        if stripped.startswith("Full thread dump"):
            start = _DUMP_START_RE.match(stripped)
            if start:
                if builder is not None:
                    yield builder.finish()
                    index += 1
                builder = _DumpBuilder(source, index, last_timestamp, start.group("vm"))
                last_timestamp = None
                continue

        if stripped[:1].isdigit():
            ts = _TIMESTAMP_RE.match(stripped)
            if ts:
                last_timestamp = _parse_timestamp(ts.group(1))
                continue
        if stripped:
            last_timestamp = None

        if builder is not None and stripped.startswith("JNI global ref"):
            # The thread list is over, but jstack prints detected deadlocks *after* this line.
            builder.ended = True
            continue

        if builder is not None and builder.ended:
            if not stripped or builder.deadlock_lines is not None or _DEADLOCK_START_RE.match(stripped):
                builder.feed(line, stripped)
                continue
            yield builder.finish()
            builder = None
            index += 1

        if builder is None:
            # Banner-less fragment: start an implicit dump at the first thread header.
            if stripped.startswith('"') and _THREAD_HEADER_RE.match(stripped):
                builder = _DumpBuilder(source, index, None, None)
            else:
                continue
        builder.feed(line, stripped)

    if builder is not None:
        yield builder.finish()
