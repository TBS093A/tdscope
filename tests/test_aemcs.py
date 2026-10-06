"""Regression tests for findings from real AEM as a Cloud Service thread dumps.

The fixture set ``fixtures/aemcs`` is an anonymized reproduction of a production data
set (see ``fixtures/make_aemcs_fixtures.py``). Each test pins down one behaviour that
was verified on the real dumps, so the analysis stays correct without access to them.
"""

import filecmp
from datetime import timezone
from pathlib import Path

import pytest

from tdscope import ThreadFilter, load
from tdscope import analysis as a

AEMCS = Path(__file__).parent / "fixtures" / "aemcs"
ALL = ThreadFilter()
SEGMENT = "org.apache.jackrabbit.oak.cache.CacheLIRS$Segment"


@pytest.fixture(scope="module")
def dumps():
    return load([str(AEMCS)])


def test_committed_fixtures_match_generator(tmp_path):
    import make_aemcs_fixtures

    make_aemcs_fixtures.main(tmp_path / "aemcs")
    cmp = filecmp.dircmp(AEMCS, tmp_path / "aemcs")
    stack = [cmp]
    while stack:
        c = stack.pop()
        assert not (c.left_only or c.right_only or c.diff_files), (
            "regenerate: python tests/fixtures/make_aemcs_fixtures.py"
        )
        stack.extend(c.subdirs.values())


# --- request threads ---------------------------------------------------------------


def test_request_threads_are_named_client_ip_start_request_line(dumps):
    requests = [t for d in dumps for t in d.threads if t.request]
    assert requests
    for t in requests:
        assert t.name.split(" ", 1)[0].count(".") == 3  # client IP prefix
        assert t.request.started_at_ms is not None


def test_jetty_acceptor_is_not_a_request(dumps):
    acceptors = [t for d in dumps for t in d.threads if "acceptor" in t.name]
    assert acceptors and all("{HTTP/1.1" in t.name and t.request is None for t in acceptors)


def test_request_age_in_utc(dumps):
    groups = {g.key: g for g in a.requests(dumps, ALL, tz=timezone.utc)}
    ref = groups["POST /libs/wcm/core/content/reference.json"]
    assert (ref.snapshots, ref.distinct_requests, ref.dumps_seen) == (12, 12, 2)
    assert ref.request_age_invalid == 0
    assert (ref.request_age_s.min, ref.request_age_s.max) == (2.0, 7.0)
    assert ref.observed_span_s is None  # every snapshot is a different request

    owner = groups["GET /sites.html/content/example/en"]
    assert (owner.request_age_s.min, owner.request_age_s.max) == (30.0, 510.0)
    assert owner.observed_span_s == 480.0


def test_thread_age_is_not_request_age(dumps):
    ref = {g.key: g for g in a.requests(dumps, ALL, tz=timezone.utc)}["POST /libs/wcm/core/content/reference.json"]
    # pooled threads are hours old although the requests are seconds old
    assert ref.thread_age_s.min > 10_000 > ref.request_age_s.max


def test_observed_span_needs_no_timezone(dumps):
    wcm = {g.key: g for g in a.requests(dumps, ThreadFilter(frame_patterns=["io.wcm."]))}
    assert list(wcm) == ["GET /content/example/de.html"]
    assert wcm["GET /content/example/de.html"].observed_span_s == 240.0


# --- JVM identity --------------------------------------------------------------------


def test_restarted_pod_is_two_jvms(dumps):
    jvms_per_pod = {}
    for d in dumps:
        pod = Path(d.source).name.rsplit("-2026", 1)[0]
        jvms_per_pod.setdefault(pod, set()).add(d.jvm_id)
    assert {pod: len(ids) for pod, ids in jvms_per_pod.items()} == {"aem-author-pod-a": 2, "aem-author-pod-b": 1}


def test_jvm_identity_is_consistent_with_uptime(dumps):
    starts = {}
    for d in dumps:
        handler = next(t for t in d.threads if t.name == "Reference Handler")
        starts.setdefault(d.jvm_id, set()).add(round(d.timestamp.timestamp() - handler.elapsed_s))
    assert all(len(s) == 1 for s in starts.values())  # one start time per detected JVM
    assert len({next(iter(s)) for s in starts.values()}) == len(starts)  # and they all differ


# --- lock contention -----------------------------------------------------------------


def test_oak_cache_segment_contention(dumps):
    report = a.locks(dumps, ALL)
    assert report.deadlocks == []
    assert len(report.contentions) == 2  # first two dumps of pod A
    for c in report.contentions:
        assert c.class_name == SEGMENT
        assert c.owner.endswith("GET /sites.html/content/example/en HTTP/1.1")
        assert c.owner_state == "RUNNABLE"
        assert len(c.waiters) == 7
        assert sum("reference.json" in w for w in c.waiters) == 6


def test_unusual_lock_lines(dumps):
    pool = next(t for t in dumps[0].threads if t.name == "OkHttp ConnectionPool")
    kinds = [(e.kind, e.address) for _, e in pool.locks]
    assert kinds == [("waiting on", None), ("waiting to re-lock in wait()", "0x00000006c0a00030")]
    assert pool.frames[3].endswith("$$Lambda$2863/0x0000000801b5a840.run(Unknown Source)")


def test_nested_locks_of_owner(dumps):
    owner = next(t for t in dumps[0].threads if t.request and t.request.method == "GET" and "sites" in t.name)
    assert [e.class_name for e in owner.held_locks()] == [SEGMENT, "java.util.concurrent.atomic.AtomicBoolean"]


# --- stuck threads -------------------------------------------------------------------


def test_stuck_reports_only_the_long_running_request(dumps):
    stuck = a.stuck(dumps, ALL)
    assert [(s.request, s.consecutive_dumps, s.span_s) for s in stuck] == [
        ("GET /sites.html/content/example/en", 3, 480.0)
    ]


def test_stuck_background_network_readers_are_opt_in(dumps):
    names = {s.name for s in a.stuck(dumps, ALL, include_network_wait=True)}
    assert {"OkHttp api.example.com", "Service Poller for status"} <= names
    # idle native frames stay excluded even then
    assert not names & {"Reference Handler", "qtp1996920089-85"} and not any("acceptor" in n for n in names)


def test_stuck_runs_do_not_cross_a_jvm_restart(dumps):
    runs = a.stuck(dumps, ALL, include_network_wait=True, min_dumps=1)
    pod_a_restarted = [r for r in runs if "pod-a_1" in r.first_seen]
    assert pod_a_restarted and all(r.consecutive_dumps == 1 for r in pod_a_restarted)


# --- CPU -----------------------------------------------------------------------------


def test_cpu_intervals_stay_within_one_jvm(dumps):
    intervals = a.cpu(dumps, ALL, top=1)
    assert len(intervals) == 4  # pod A: 2 (restart breaks the series), pod B: 2
    assert all(i.interval_s == 240.0 for i in intervals)
    pod_a = [i for i in intervals if "pod-a" in i.start]
    assert all(i.top[0].name.endswith("GET /sites.html/content/example/en HTTP/1.1") for i in pod_a)
    assert all(i.top[0].cpu_ms == 120_000.0 and i.top[0].wall_pct == 50.0 for i in pod_a)
