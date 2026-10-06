"""Data model for parsed HotSpot thread dumps."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from datetime import datetime


@dataclass(frozen=True, slots=True)
class LockEvent:
    """A lock-related line attached to a stack frame (``- locked <0x..> (a Foo)``)."""

    kind: str  # "locked", "waiting to lock", "waiting on", "parking to wait for", ...
    address: str | None
    class_name: str | None


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """HTTP request information embedded in a thread name by the servlet container.

    Jetty (and AEM / Sling on top of it) renames request threads, e.g.
    ``qtp123-45 [1700000000000] GET /content/page.html HTTP/1.1``.
    """

    method: str
    path: str
    protocol: str
    started_at_ms: int | None = None  # epoch millis, when present in the thread name

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(slots=True)
class ThreadInfo:
    name: str
    header: str
    number: int | None = None
    daemon: bool = False
    prio: int | None = None
    os_prio: int | None = None
    cpu_ms: float | None = None
    elapsed_s: float | None = None  # thread age (NOT request duration)
    tid: str | None = None
    nid: int | None = None
    status: str = ""  # free text after the attributes, e.g. "waiting on condition"
    state: str | None = None  # java.lang.Thread.State, e.g. "TIMED_WAITING"
    state_detail: str | None = None  # e.g. "parking", "on object monitor"
    frames: list[str] = field(default_factory=list)
    locks: list[tuple[int, LockEvent]] = field(default_factory=list)  # (frame index, event)
    owned_synchronizers: list[LockEvent] = field(default_factory=list)
    request: HttpRequest | None = None

    @property
    def stack_text(self) -> str:
        """The stack rendered back in jstack-like form, lock lines included."""
        by_frame: dict[int, list[LockEvent]] = {}
        for idx, ev in self.locks:
            by_frame.setdefault(idx, []).append(ev)
        out = []
        for i, frame in enumerate(self.frames):
            out.append(f"\tat {frame}")
            for ev in by_frame.get(i, []):
                target = f"<{ev.address}>" if ev.address else ""
                cls = f" (a {ev.class_name})" if ev.class_name else ""
                out.append(f"\t- {ev.kind} {target}{cls}".rstrip())
        return "\n".join(out)

    def waiting_for(self) -> LockEvent | None:
        """The lock this thread is blocked on / waiting for, if any (top-most frame)."""
        for _, ev in self.locks:
            if ev.kind in ("waiting to lock", "waiting on", "parking to wait for", "waiting to re-lock in wait()"):
                return ev
        return None

    def held_locks(self) -> list[LockEvent]:
        held = [ev for _, ev in self.locks if ev.kind == "locked"]
        return held + list(self.owned_synchronizers)


@dataclass
class Deadlock:
    """A deadlock reported by the JVM itself ("Found one Java-level deadlock")."""

    threads: list[str]
    text: str


@dataclass
class ThreadDump:
    source: str  # file path (or "<stdin>")
    index: int  # n-th dump inside the source file
    timestamp: datetime | None
    vm_info: str | None
    threads: list[ThreadInfo] = field(default_factory=list)
    deadlocks: list[Deadlock] = field(default_factory=list)

    @cached_property
    def jvm_id(self) -> str:
        """Best-effort identity of the JVM process that produced this dump.

        Uses the native address (``tid``) of a thread created at JVM start-up: it is
        stable for the lifetime of the process and differs between processes.
        """
        for name in ("Reference Handler", "Signal Dispatcher", "Finalizer", "main"):
            for thread in self.threads:
                if thread.name == name and thread.tid:
                    return f"{self.vm_info or '?'} / {name} tid={thread.tid}"
        return f"{self.vm_info or '?'} / {self.source}"

    @property
    def label(self) -> str:
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.timestamp else "no timestamp"
        suffix = f"#{self.index + 1}" if self.index else ""
        return f"{self.source}{suffix} ({ts})"
