"""Generate a synthetic demo data set: two AEM-like author JVMs, 30 thread dumps each.

Everything is made up (documentation IP ranges, example.com, open-source class names),
so the output is safe to share. It is used for the screenshots in docs/TUI.md and to
try the TUI without dumps of your own:

    python docs/demo/make_demo_dumps.py demo-dumps
    tdscope tui demo-dumps --tz UTC

The story in the data: from minute 8 to 18 a long request on author-a holds an Oak cache
segment and reference.json requests pile up behind it; Jetty and job pools breathe;
plenty of small thread families end up in the chart's "other" group.
"""

from __future__ import annotations

import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "fixtures"))

from make_aemcs_fixtures import (
    IDLE_POOL_FRAMES,
    SLING_REQUEST,
    J,
    Jvm,
    Thread,
    base_threads,
    blocked_on_segment,
    epoch_ms,
    lock_owner,
    render,
    vm_threads,
)

DUMPS = 30
UTC = timezone.utc
EXECUTOR_IDLE = [
    f"at jdk.internal.misc.Unsafe.park({J}/Native Method)",
    "- parking to wait for  <0x00000006c1000040> "
    "(a java.util.concurrent.locks.AbstractQueuedSynchronizer$ConditionObject)",
    f"at java.util.concurrent.locks.LockSupport.park({J}/LockSupport.java:194)",
    f"at java.util.concurrent.LinkedBlockingQueue.take({J}/LinkedBlockingQueue.java:433)",
    f"at java.util.concurrent.ThreadPoolExecutor.getTask({J}/ThreadPoolExecutor.java:1054)",
    f"at java.util.concurrent.ThreadPoolExecutor$Worker.run({J}/ThreadPoolExecutor.java:628)",
    f"at java.lang.Thread.run({J}/Thread.java:834)",
]
SCHEDULED_IDLE = [
    f"at jdk.internal.misc.Unsafe.park({J}/Native Method)",
    f"at java.util.concurrent.locks.LockSupport.parkNanos({J}/LockSupport.java:234)",
    f"at java.util.concurrent.ScheduledThreadPoolExecutor$DelayedWorkQueue.take({J}/ScheduledThreadPoolExecutor.java:1182)",
    f"at java.util.concurrent.ThreadPoolExecutor.getTask({J}/ThreadPoolExecutor.java:1054)",
    f"at java.lang.Thread.run({J}/Thread.java:834)",
]
TIMER_IDLE = [
    f"at java.lang.Object.wait({J}/Native Method)",
    "- waiting on <0x00000006c1000080> (a java.util.TaskQueue)",
    f"at java.util.TimerThread.mainLoop({J}/Timer.java:553)",
    f"at java.util.TimerThread.run({J}/Timer.java:506)",
]


def stable_threads(n: int, jvm_name: str) -> list[Thread]:
    """Threads present in every dump of a JVM (keeps native addresses stable)."""
    threads = base_threads(n)
    threads += [Thread(f"pool-3-thread-{i}", 200 + i, 500.0, "WAITING (parking)", EXECUTOR_IDLE) for i in range(1, 7)]
    threads += [
        Thread(f"sling-default-{i}", 220 + i, 60.0, "TIMED_WAITING (parking)", SCHEDULED_IDLE) for i in range(1, 6)
    ]
    for i in range(1, 4):
        threads.append(
            Thread(
                f"sling-oak-observation-{i}",
                240 + i,
                90.0,
                "WAITING (parking)",
                [
                    f"at jdk.internal.misc.Unsafe.park({J}/Native Method)",
                    "at org.apache.jackrabbit.oak.plugins.observation.ChangeProcessor$4.call(ChangeProcessor.java:400)",
                    *EXECUTOR_IDLE[4:],
                ],
            )
        )
    threads += [
        Thread(f"EventAdminAsyncThread #{i}", 260 + i, 70.0, "WAITING (parking)", EXECUTOR_IDLE) for i in range(1, 5)
    ]
    misc = [
        ("logback-1", SCHEDULED_IDLE),
        ("Timer-0", TIMER_IDLE),
        ("Timer-1", TIMER_IDLE),
        ("HealthCheck-idle", SCHEDULED_IDLE),
        ("CleanCursors-1-thread-1", SCHEDULED_IDLE),
        (
            "ForkJoinPool.commonPool-worker-1",
            [
                f"at jdk.internal.misc.Unsafe.park({J}/Native Method)",
                f"at java.util.concurrent.ForkJoinPool.awaitWork({J}/ForkJoinPool.java:1628)",
                f"at java.util.concurrent.ForkJoinPool.runWorker({J}/ForkJoinPool.java:1592)",
            ],
        ),
        (
            "I/O dispatcher 1",
            [
                f"at sun.nio.ch.EPoll.wait({J}/Native Method)",
                "at org.apache.http.impl.nio.reactor.AbstractIOReactor.execute(AbstractIOReactor.java:255)",
                f"at java.lang.Thread.run({J}/Thread.java:834)",
            ],
        ),
        (
            "MVStore background writer /opt/aem/repository/cache-1.data",
            [
                f"at java.lang.Object.wait({J}/Native Method)",
                "at org.h2.mvstore.MVStore$BackgroundWriterThread.run(MVStore.java:2900)",
            ],
        ),
        ("async-index-update-async", SCHEDULED_IDLE),
        ("discovery-poller-1", SCHEDULED_IDLE),
        ("cluster-ClusterId-1", SCHEDULED_IDLE),
        ("oak-lucene-1", EXECUTOR_IDLE),
        ("oak-lucene-2", EXECUTOR_IDLE),
        ("RenditionMetadataListenerService-1", EXECUTOR_IDLE),
        ("deletion tracker", TIMER_IDLE),
        ("NewsFeed-Notifier-1", SCHEDULED_IDLE),
    ]
    threads += [
        Thread(
            name,
            300 + i,
            120.0,
            "TIMED_WAITING (parking)" if "park" in frames[0] else "WAITING (on object monitor)",
            frames,
        )
        for i, (name, frames) in enumerate(misc)
    ]
    return threads


def job_threads(rng: random.Random, n: int) -> list[Thread]:
    """Sling job pool (UUID in the name): busy share follows a wave."""
    uuid = "0f1e2d3c-4b5a-4697-8877-665544332211"
    busy = round(3 + 3 * math.sin(n / 4))
    out = []
    for i in range(1, 9):
        name = f"sling-threadpool-{uuid}-(apache-sling-job-thread-pool)-{i}"
        if i <= busy:
            frames = [
                "at org.apache.jackrabbit.oak.plugins.document.DocumentNodeStore.getNode(DocumentNodeStore.java:1384)",
                "at org.apache.sling.event.impl.jobs.queues.JobQueueImpl$1.run(JobQueueImpl.java:300)",
                *EXECUTOR_IDLE[4:],
            ]
            out.append(
                Thread(
                    name,
                    400 + i,
                    200.0,
                    "RUNNABLE",
                    frames,
                    status="runnable",
                    cpu_ms=500.0 * n * i + rng.random() * 100,
                )
            )
        else:
            out.append(Thread(name, 400 + i, 200.0, "WAITING (parking)", EXECUTOR_IDLE))
    return out


def jetty_threads(rng: random.Random, n: int, at: datetime, burst: bool) -> list[Thread]:
    """Jetty workers: some serve short requests, the rest idle; pool size breathes."""
    out = []
    size = 10 + round(6 * math.sin(n / 3)) + (6 if burst else 0)
    for i in range(size):
        name = f"qtp1996920089-{500 + i}"
        if rng.random() < 0.25:
            started = at - timedelta(milliseconds=rng.randint(80, 2500))
            ip = f"203.0.113.{rng.randint(2, 250)}"
            path = rng.choice(
                [
                    "/content/site/en.html",
                    "/content/site/de/products.html",
                    "/content/dam/hero.jpg",
                    "/libs/granite/csrf/token.json",
                ]
            )
            frames = [
                "at io.wcm.handler.link.impl.LinkHandlerImpl.get(LinkHandlerImpl.java:88)",
                "at com.example.site.components.Teaser.init(Teaser.java:31)",
                *SLING_REQUEST,
            ]
            out.append(
                Thread(
                    f"{ip} [{epoch_ms(started)}] GET {path} HTTP/1.1",
                    500 + i,
                    300.0,
                    "RUNNABLE",
                    frames,
                    status="runnable",
                    cpu_ms=rng.randint(5, 900),
                )
            )
        else:
            out.append(Thread(name, 500 + i, 300.0, "TIMED_WAITING (parking)", IDLE_POOL_FRAMES))
    return out


def main(out: Path) -> None:
    rng = random.Random(7)
    t0 = datetime(2026, 5, 12, 10, 0, 0)
    for jvm_name, offset, tid_base in (("author-a", 0, 0x00007F9AC8188800), ("author-b", 20, 0x00007F6CCC188800)):
        jvm = Jvm(start=t0 - timedelta(hours=6), tid_base=tid_base)
        directory = out / jvm_name
        directory.mkdir(parents=True, exist_ok=True)
        owner_start = (t0 + timedelta(minutes=8) - timedelta(seconds=40)).replace(tzinfo=UTC)
        for n in range(DUMPS):
            at = t0 + timedelta(minutes=n, seconds=offset)
            burst = jvm_name == "author-a" and 8 <= n <= 18
            threads = stable_threads(n, jvm_name) + job_threads(rng, n)
            if burst:
                threads.append(lock_owner(owner_start, 40_000.0 + 55_000.0 * (n - 8)))
                waiting = round(2 + 18 * math.sin(math.pi * (n - 8) / 10))
                for i in range(waiting):
                    started = at.replace(tzinfo=UTC) - timedelta(seconds=rng.uniform(0.5, 45))
                    threads.append(
                        blocked_on_segment(
                            f"198.51.100.{20 + i} [{epoch_ms(started)}] "
                            "POST /libs/wcm/core/content/reference.json HTTP/1.1",
                            700 + i,
                        )
                    )
            threads += jetty_threads(rng, n, at.replace(tzinfo=UTC), burst)
            (directory / f"{jvm_name}-{at:%Y-%m-%d.%H-%M-%S}.dump").write_bytes(
                render(jvm, at, threads + vm_threads()).encode()
            )


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-dumps")
    main(target)
    print(f"written {2 * DUMPS} dumps to {target}")
