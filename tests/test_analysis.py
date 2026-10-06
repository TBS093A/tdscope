from datetime import timezone

import pytest

from tdscope import ThreadFilter
from tdscope import analysis as a

WCM = ThreadFilter(frame_patterns=["io.wcm."])


def test_summary(all_dumps):
    result = a.summary(all_dumps, ThreadFilter())
    assert len(result) == 5
    deadlocked = [s for s in result if s.deadlocks]
    assert [s.deadlocks for s in deadlocked] == [[["worker-A", "worker-B"]]]


def test_requests_grouping_and_age(aem_dumps):
    groups = {g.key: g for g in a.requests(aem_dumps, WCM, tz=timezone.utc)}
    assert set(groups) == {"GET /content/site/en.html", "GET /content/site/de.html", "POST /bin/api/search"}

    en = groups["GET /content/site/en.html"]
    assert (en.snapshots, en.dumps_seen, en.distinct_requests) == (3, 3, 1)
    assert en.request_age_s is not None
    assert (en.request_age_s.min, en.request_age_s.max) == (5.0, 25.0)
    assert en.observed_span_s == 20.0
    assert en.thread_age_s is not None and en.thread_age_s.avg == 1200.5
    assert list(en.top_frames) == ["io.wcm.handler.url.impl.UrlHandlerImpl.externalize(UrlHandlerImpl.java:120)"]


def test_requests_keep_query(aem_dumps):
    keys = {g.key for g in a.requests(aem_dumps, ThreadFilter(), keep_query=True, tz=timezone.utc)}
    assert "POST /bin/api/search?q=foo" in keys


def test_requests_wrong_timezone_is_reported(aem_dumps):
    from datetime import timedelta

    groups = a.requests(aem_dumps, WCM, tz=timezone(timedelta(hours=-5)))  # ages look 5 h too long
    assert all(g.request_age_invalid == 0 for g in groups)
    groups = a.requests(aem_dumps, WCM, tz=timezone(timedelta(hours=5)))  # dumps "before" requests
    assert all(g.request_age_s is None and g.request_age_invalid > 0 for g in groups)


def test_frames(aem_dumps):
    result = a.frames(aem_dumps, WCM)
    assert result[0].frame.startswith("io.wcm.handler.link.impl.LinkHandlerImpl.get")
    assert (result[0].snapshots, result[0].distinct_threads) == (4, 2)

    top_only = a.frames(aem_dumps, WCM, top_only=True)
    assert all("LinkHandlerImpl" not in g.frame for g in top_only)


def test_frames_requires_patterns(aem_dumps):
    with pytest.raises(ValueError):
        a.frames(aem_dumps, ThreadFilter())


def test_hotspots(aem_dumps):
    result = a.hotspots(aem_dumps, ThreadFilter(), depth=3)
    top = result[0]
    assert top.snapshots == 3 and top.distinct_threads == 1  # same thread seen in three dumps


def test_stuck(all_dumps):
    result = a.stuck(all_dumps, ThreadFilter())
    assert [s.request for s in result] == ["GET /content/site/en.html"]
    assert (result[0].consecutive_dumps, result[0].span_s) == (3, 20.0)


def test_stuck_all_states_includes_idle_workers(aem_dumps):
    names = {s.name for s in a.stuck(aem_dumps, ThreadFilter(), all_states=True)}
    assert {"qtp1001-103", "batch-worker-1", "batch-worker-2"} <= names


def test_locks(all_dumps):
    report = a.locks(all_dumps, ThreadFilter())
    assert report.deadlocks == [(all_dumps[-1].label, ["worker-A", "worker-B"])]
    by_addr = {}
    for c in report.contentions:
        by_addr.setdefault(c.address, []).append(c)
    monitor = by_addr["0x00000000c0ffee00"]
    assert len(monitor) == 1 and monitor[0].owner.startswith("qtp1001-101")
    reentrant = by_addr["0x00000000c2000000"]
    assert len(reentrant) == 3 and {c.owner for c in reentrant} == {"batch-worker-1"}
    # idle pool workers parked on a Condition are not contention
    assert "0x00000000c1000000" not in by_addr


def test_cpu(all_dumps):
    intervals = a.cpu(all_dumps, ThreadFilter(), top=1)
    assert len(intervals) == 2  # only consecutive dumps of the same JVM are compared
    for interval in intervals:
        assert interval.interval_s == 10.0
        (top,) = interval.top
        assert top.name.startswith("qtp1001-101") and (top.cpu_ms, top.wall_pct) == (8000.0, 80.0)
