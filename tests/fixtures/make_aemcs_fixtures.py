"""Generate the anonymized AEM as a Cloud Service fixture set in ``tests/fixtures/aemcs``.

The layout and thread shapes reproduce what real AEMaaCS author thread dumps look like
(observed on a production data set that is not part of this repository):

* one directory per pod and collection window, dumps every 4 minutes, JVM in UTC;
* request threads named ``<client ip> [<start epoch ms>] METHOD /path HTTP/1.1``;
* a Jetty acceptor whose name contains ``{HTTP/1.1, ...}`` but is not a request;
* background threads RUNNABLE in ``socketRead0`` by design (OkHttp HTTP/2 reader,
  replication "Service Poller" long polling);
* lock lines without an address (``waiting on <no object reference available>``),
  ``waiting to re-lock in wait()`` and lambda frames ``(Unknown Source)``;
* many ``reference.json`` requests BLOCKED on one Oak ``CacheLIRS$Segment`` monitor held
  by a long-running request;
* a pod whose JVM restarted between two collection windows.

Only IP documentation ranges (RFC 5737), example.com and open-source class names are used.
Regenerate with ``python tests/fixtures/make_aemcs_fixtures.py`` (output is deterministic).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).parent / "aemcs"
T = "\t"
J = "java.base@11.0.19"

OAK_READ = [
    "at org.apache.jackrabbit.oak.cache.CacheLIRS.get(CacheLIRS.java:291)",
    "at org.apache.jackrabbit.oak.plugins.document.DocumentNodeStore.getNode(DocumentNodeStore.java:1384)",
    "at org.apache.jackrabbit.oak.plugins.document.DocumentNodeState.getChildNodeDoc(DocumentNodeState.java:550)",
    "at org.apache.jackrabbit.oak.plugins.document.DocumentNodeState.hasChildNode(DocumentNodeState.java:267)",
    "at org.apache.jackrabbit.oak.plugins.tree.impl.AbstractTree.hasChild(AbstractTree.java:293)",
    "at org.apache.jackrabbit.oak.security.authorization.permission.PermissionStoreImpl.load(PermissionStoreImpl.java:85)",
]
SLING_REQUEST = [
    "at org.apache.sling.engine.impl.SlingRequestProcessorImpl.processRequest(SlingRequestProcessorImpl.java:151)",
    "at org.apache.sling.engine.impl.SlingMainServlet.service(SlingMainServlet.java:156)",
    "at org.eclipse.jetty.server.HttpChannel.handle(HttpChannel.java:501)",
    "at org.eclipse.jetty.util.thread.QueuedThreadPool$Runner.run(QueuedThreadPool.java:1034)",
    f"at java.lang.Thread.run({J}/Thread.java:834)",
]
SEGMENT = "org.apache.jackrabbit.oak.cache.CacheLIRS$Segment"
IDLE_POOL_FRAMES = [
    f"at jdk.internal.misc.Unsafe.park({J}/Native Method)",
    "- parking to wait for  <0x00000006c0a00020> "
    "(a java.util.concurrent.locks.AbstractQueuedSynchronizer$ConditionObject)",
    f"at java.util.concurrent.locks.LockSupport.parkNanos({J}/LockSupport.java:234)",
    "at org.eclipse.jetty.util.BlockingArrayQueue.poll(BlockingArrayQueue.java:382)",
    "at org.eclipse.jetty.util.thread.QueuedThreadPool$Runner.run(QueuedThreadPool.java:1018)",
    f"at java.lang.Thread.run({J}/Thread.java:834)",
]


@dataclass
class Thread:
    name: str
    number: int | None
    created_s: float  # seconds after JVM start
    state: str | None
    frames: list[str] = field(default_factory=list)
    status: str = "waiting on condition"
    daemon: bool = False
    prio: int = 5
    cpu_ms: float = 10.0
    syncs: list[str] = field(default_factory=list)


@dataclass
class Jvm:
    start: datetime
    tid_base: int


def render(jvm: Jvm, at: datetime, threads: list[Thread]) -> str:
    uptime = (at - jvm.start).total_seconds()
    lines = [
        at.strftime("%Y-%m-%d %H:%M:%S"),
        "Full thread dump Java HotSpot(TM) 64-Bit Server VM (11.0.19+9-LTS-224 mixed mode):",
        "",
        "Threads class SMR info:",
        f"_java_thread_list=0x00005607d71a75d0, length={len(threads)}, elements={{",
    ]
    tids = [f"0x{jvm.tid_base + 0x800 * i:016x}" for i in range(len(threads))]
    for i in range(0, len(tids), 4):
        lines.append(", ".join(tids[i : i + 4]) + ("," if i + 4 < len(tids) else ""))
    lines += ["}", ""]
    for i, t in enumerate(threads):
        number = f"#{t.number} " if t.number is not None else ""
        daemon = "daemon " if t.daemon else ""
        prio = f"prio={t.prio} " if t.number is not None else ""
        elapsed = uptime - t.created_s
        stack_addr = f"[0x00007f99{0x1000 * i:08x}]" if t.frames else ""
        lines.append(
            f'"{t.name}" {number}{daemon}{prio}os_prio=0 cpu={t.cpu_ms:.2f}ms elapsed={elapsed:.2f}s '
            f"tid={tids[i]} nid=0x{0x2D00 + i:x} {t.status}  {stack_addr}"
        )
        if t.state:
            lines.append(f"   java.lang.Thread.State: {t.state}")
            lines += [f"{T}{f}" for f in t.frames]
            lines += ["", "   Locked ownable synchronizers:"]
            lines += [f"{T}- {s}" for s in t.syncs] or [f"{T}- None"]
        lines.append("")
    lines += ["JNI global refs: 98, weak refs: 3", "", ""]
    return "\n".join(lines)


def base_threads(dump_no: int) -> list[Thread]:
    return [
        Thread(
            "Reference Handler",
            2,
            0.1,
            "RUNNABLE",
            daemon=True,
            prio=10,
            status="waiting on condition",
            frames=[
                f"at java.lang.ref.Reference.waitForReferencePendingList({J}/Native Method)",
                f"at java.lang.ref.Reference.processPendingReferences({J}/Reference.java:241)",
                f"at java.lang.ref.Reference$ReferenceHandler.run({J}/Reference.java:213)",
            ],
        ),
        Thread("Signal Dispatcher", 4, 0.1, "RUNNABLE", daemon=True, prio=9, status="runnable"),
        Thread(
            "qtp1996920089-84-acceptor-0@4bd63113-ServerConnector@b308372{HTTP/1.1, (http/1.1)}{0.0.0.0:8080}",
            84,
            40.0,
            "RUNNABLE",
            prio=3,
            status="runnable",
            cpu_ms=830.85,
            frames=[
                f"at sun.nio.ch.ServerSocketChannelImpl.accept0({J}/Native Method)",
                f"at sun.nio.ch.ServerSocketChannelImpl.accept({J}/ServerSocketChannelImpl.java:533)",
                "at org.eclipse.jetty.server.ServerConnector.accept(ServerConnector.java:388)",
                f"at java.lang.Thread.run({J}/Thread.java:834)",
            ],
        ),
        Thread(
            "qtp1996920089-85",
            85,
            40.0,
            "RUNNABLE",
            status="runnable",
            frames=[
                f"at sun.nio.ch.EPoll.wait({J}/Native Method)",
                f"at sun.nio.ch.EPollSelectorImpl.doSelect({J}/EPollSelectorImpl.java:120)",
                "- locked <0x00000006c0a00010> (a sun.nio.ch.Util$2)",
                "at org.eclipse.jetty.io.ManagedSelector.nioSelect(ManagedSelector.java:183)",
                f"at java.lang.Thread.run({J}/Thread.java:834)",
            ],
        ),
        Thread(
            "qtp1996920089-90",
            90,
            40.0,
            "TIMED_WAITING (parking)",
            frames=IDLE_POOL_FRAMES,
        ),
        Thread(
            "OkHttp api.example.com",
            845,
            600.0,
            "RUNNABLE",
            daemon=True,
            status="runnable",
            cpu_ms=2.5 + dump_no,
            frames=[
                f"at java.net.SocketInputStream.socketRead0({J}/Native Method)",
                f"at java.net.SocketInputStream.socketRead({J}/SocketInputStream.java:115)",
                f"at java.net.SocketInputStream.read({J}/SocketInputStream.java:168)",
                "at okio.Okio$2.read(Okio.java:140)",
                "at okhttp3.internal.http2.Http2Reader.nextFrame(Http2Reader.java:89)",
                "at okhttp3.internal.http2.Http2Connection$ReaderRunnable.execute(Http2Connection.java:577)",
                "at okhttp3.internal.NamedRunnable.run(NamedRunnable.java:32)",
                f"at java.lang.Thread.run({J}/Thread.java:834)",
            ],
        ),
        Thread(
            "OkHttp ConnectionPool",
            846,
            600.0,
            "TIMED_WAITING (on object monitor)",
            daemon=True,
            status="in Object.wait()",
            frames=[
                f"at java.lang.Object.wait({J}/Native Method)",
                "- waiting on <no object reference available>",
                f"at java.lang.Object.wait({J}/Object.java:462)",
                "at okhttp3.internal.connection.RealConnectionPool.lambda$new$0(RealConnectionPool.java:62)",
                "- waiting to re-lock in wait() <0x00000006c0a00030> "
                "(a okhttp3.internal.connection.RealConnectionPool)",
                "at okhttp3.internal.connection.RealConnectionPool$$Lambda$2863/0x0000000801b5a840.run(Unknown Source)",
                f"at java.util.concurrent.ThreadPoolExecutor.runWorker({J}/ThreadPoolExecutor.java:1128)",
                f"at java.lang.Thread.run({J}/Thread.java:834)",
            ],
        ),
        Thread(
            "Service Poller for status",
            296,
            100.0,
            "RUNNABLE",
            daemon=True,
            status="runnable",
            cpu_ms=172.67,
            frames=[
                f"at java.net.SocketInputStream.socketRead0({J}/Native Method)",
                f"at java.net.SocketInputStream.socketRead({J}/SocketInputStream.java:115)",
                "at org.apache.http.impl.io.SessionInputBufferImpl.fillBuffer(SessionInputBufferImpl.java:153)",
                "at org.apache.http.impl.conn.DefaultHttpResponseParser.parseHead(DefaultHttpResponseParser.java:138)",
                "at org.apache.http.impl.execchain.MainClientExec.execute(MainClientExec.java:272)",
                f"at java.lang.Thread.run({J}/Thread.java:834)",
            ],
        ),
    ]


def vm_threads() -> list[Thread]:
    return [
        Thread("VM Thread", None, 0.0, None, status="runnable", cpu_ms=3300.0),
        Thread("GC Thread#0", None, 0.0, None, status="runnable", cpu_ms=9100.0),
        Thread("VM Periodic Task Thread", None, 0.0, None, cpu_ms=3308.4),
    ]


def epoch_ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def lock_owner(start: datetime, cpu_ms: float) -> Thread:
    """Long-running request holding the Oak cache segment monitor."""
    return Thread(
        f"192.0.2.10 [{epoch_ms(start)}] GET /sites.html/content/example/en HTTP/1.1",
        375,
        300.0,
        "RUNNABLE",
        status="runnable",
        cpu_ms=cpu_ms,
        frames=[
            f"at {SEGMENT}.put(CacheLIRS.java:1155)",
            f"- locked <0x00000006d1000000> (a {SEGMENT})",
            f"at {SEGMENT}.load(CacheLIRS.java:1028)",
            f"at {SEGMENT}.get(CacheLIRS.java:980)",
            "- locked <0x00000006d1000100> (a java.util.concurrent.atomic.AtomicBoolean)",
            *OAK_READ,
            *SLING_REQUEST,
        ],
    )


def blocked_on_segment(name: str, number: int) -> Thread:
    return Thread(
        name,
        number,
        300.0,
        "BLOCKED (on object monitor)",
        status="waiting for monitor entry",
        frames=[
            f"at {SEGMENT}.access(CacheLIRS.java:902)",
            f"- waiting to lock <0x00000006d1000000> (a {SEGMENT})",
            f"at {SEGMENT}.get(CacheLIRS.java:885)",
            *OAK_READ,
            *SLING_REQUEST,
        ],
    )


def reference_requests(at: datetime, count: int) -> list[Thread]:
    """``count`` reference.json requests started 2..(count+1) seconds before the dump."""
    return [
        blocked_on_segment(
            f"198.51.100.{20 + i} [{epoch_ms(at - timedelta(seconds=2 + i))}] "
            "POST /libs/wcm/core/content/reference.json HTTP/1.1",
            400 + i,
        )
        for i in range(count)
    ]


def wcm_request(start: datetime) -> Thread:
    return Thread(
        f"203.0.113.7 [{epoch_ms(start)}] GET /content/example/de.html HTTP/1.1",
        377,
        300.0,
        "TIMED_WAITING (parking)",
        frames=[
            f"at jdk.internal.misc.Unsafe.park({J}/Native Method)",
            "- parking to wait for  <0x00000006c0b00000> (a java.util.concurrent.CompletableFuture$Signaller)",
            f"at java.util.concurrent.CompletableFuture.timedGet({J}/CompletableFuture.java:1939)",
            "at io.wcm.caconfig.extensions.persistence.impl.PagePersistenceStrategy.load(PagePersistenceStrategy.java:91)",
            *SLING_REQUEST,
        ],
    )


def write(out: Path, pod: str, window: int, jvm: Jvm, at: datetime, threads: list[Thread]) -> None:
    directory = out / f"aem-author-{pod}_{window}_threaddumps"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"aem-author-{pod}-{at:%Y-%m-%d.%H-%M-%S}.dump").write_bytes(render(jvm, at, threads).encode())


def main(out: Path = OUT) -> None:
    shutil.rmtree(out, ignore_errors=True)
    t0 = datetime(2026, 4, 26, 12, 0, 0)
    utc = timezone.utc

    # Pod A, window 0: three dumps of one JVM; reference.json requests pile up on the
    # segment held by a request that keeps running (stuck) across all three dumps.
    jvm_a1 = Jvm(start=t0 - timedelta(hours=3), tid_base=0x00007F9AC8188800)
    owner_start = (t0 - timedelta(seconds=30)).replace(tzinfo=utc)
    waiters = {0: 6, 1: 6, 2: 0}
    for n in range(3):
        at = t0 + timedelta(minutes=4 * n)
        threads = [*base_threads(n), lock_owner(owner_start, 55_000.0 + 120_000.0 * n)]
        threads += reference_requests(at.replace(tzinfo=utc), waiters[n])
        if waiters[n]:
            threads.append(blocked_on_segment("sling-default-3-Registered Service.1", 520))
        write(out, "pod-a", 0, jvm_a1, at, threads + vm_threads())

    # Pod A, window 1: the JVM restarted (new start time, new native addresses).
    restart = t0 + timedelta(minutes=12)
    jvm_a2 = Jvm(start=restart, tid_base=0x00007F5338188800)
    write(out, "pod-a", 1, jvm_a2, restart + timedelta(minutes=4), base_threads(0) + vm_threads())

    # Pod B: same JVM version, different process; an io.wcm request seen in two dumps.
    jvm_b = Jvm(start=t0 - timedelta(hours=1), tid_base=0x00007F6CCC188800)
    wcm_start = (t0 + timedelta(seconds=10)).replace(tzinfo=utc)
    for n in range(3):
        at = t0 + timedelta(minutes=4 * n, seconds=30)
        threads = base_threads(n) + ([wcm_request(wcm_start)] if n < 2 else [])
        write(out, "pod-b", 0, jvm_b, at, threads + vm_threads())


if __name__ == "__main__":
    main()
    print(f"written to {OUT}")
