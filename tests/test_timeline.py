import json
import re
from collections import Counter
from datetime import timedelta, timezone

import pytest

from tdscope import ThreadFilter, load
from tdscope.timeline import (
    OTHER,
    OTHER_COLOR,
    build_chart,
    describe,
    format_duration,
    group_of,
    pool_name,
    to_html,
)

UTC = timezone.utc


@pytest.fixture(scope="module")
def aemcs(fixtures_dir=None):
    from pathlib import Path

    return load([str(Path(__file__).parent / "fixtures" / "aemcs")])


def _thread(dumps, prefix):
    return next(t for d in dumps for t in d.threads if t.name.startswith(prefix))


def test_grouping(aemcs):
    acceptor = _thread(aemcs, "qtp1996920089-84-acceptor")
    request = _thread(aemcs, "198.51.100.20")
    assert group_of(acceptor, "pool") == "qtpN-N-acceptor-N"
    assert group_of(_thread(aemcs, "sling-default"), "pool") == "sling-default-N-Registered Service.N"
    assert group_of(request, "pool") == "HTTP requests"
    assert group_of(request, "request") == "POST /libs/wcm/core/content/reference.json"
    assert group_of(acceptor, "request") is None
    assert group_of(_thread(aemcs, "VM Thread"), "state") == "(VM internal)"
    with pytest.raises(ValueError):
        group_of(acceptor, "nope")


@pytest.mark.parametrize("by", ["pool", "state"])
def test_count_mode_accounts_for_every_thread(aemcs, by):
    chart = build_chart(aemcs, mode="count", group_by=by)
    assert chart.dumps == len(aemcs)
    assert all(len(s.points) == len(aemcs) for s in chart.series)
    per_time = Counter()
    for p in chart.points:
        per_time[(p.time, p.dump.source)] += p.value
    assert sorted(per_time.values()) == sorted(float(len(d.threads)) for d in aemcs)


def test_count_mode_respects_filter(aemcs):
    chart = build_chart(aemcs, ThreadFilter(states=["BLOCKED"]), mode="count", group_by="state")
    assert [s.group for s in chart.series] == ["BLOCKED"]
    assert max(p.value for p in chart.points) == 7.0


def test_duration_mode_uses_request_age_then_thread_age(aemcs):
    chart = build_chart(aemcs, mode="duration", group_by="pool", tz=UTC)
    assert chart.invalid_request_ages == 0
    owner = sorted(p.value for p in chart.points if p.threads[0].name.startswith("192.0.2.10"))
    assert owner == [30.0, 270.0, 510.0]
    assert all(p.duration_kind == "request age" for p in chart.points if p.threads[0].request)
    handler = [p for p in chart.points if p.threads[0].name == "Reference Handler"]
    assert handler and all(
        p.duration_kind == "thread age" and p.value > 200 for p in handler
    )  # 4 min after the restart


def test_duration_mode_flags_wrong_time_zone(aemcs):
    chart = build_chart(aemcs, mode="duration", tz=timezone(timedelta(hours=5)))
    requests = sum(1 for d in aemcs for t in d.threads if t.request)
    assert chart.invalid_request_ages == requests
    assert not any(p.duration_kind == "request age" for p in chart.points)


def test_small_groups_are_merged_into_other(aemcs):
    full = build_chart(aemcs, mode="count", group_by="pool")
    capped = build_chart(aemcs, mode="count", group_by="pool", max_groups=3)
    assert [s.group for s in capped.series][-1] == OTHER
    assert capped.series[-1].color == OTHER_COLOR
    assert len({s.color for s in capped.series}) == 3
    assert sum(s.total for s in capped.series) == sum(s.total for s in full.series)


def test_axes(aemcs):
    chart = build_chart(aemcs, mode="duration", tz=UTC)
    assert chart.x_fraction(chart.start) == 0.0 and chart.x_fraction(chart.end) == 1.0
    fractions = [chart.y_fraction(v) for v in (0.5, 10, 600, 3600)]
    assert fractions == sorted(fractions) and fractions[0] >= 0 and fractions[-1] <= 1
    assert [label for _, label in chart.y_ticks()][:4] == ["1s", "10s", "1m", "10m"]
    count = build_chart(aemcs, mode="count")
    assert count.y_ticks()[0] == (0.0, "0")


def test_format_duration():
    assert [format_duration(s) for s in (5, 65, 3700, 90000)] == ["5.0s", "1m05s", "1h01m", "1d01h"]


def test_describe(aemcs):
    chart = build_chart(aemcs, mode="duration", tz=UTC)
    point = next(p for p in chart.points if p.threads[0].name.startswith("192.0.2.10"))
    text = describe(point)
    assert "request age: 30.0s" in text and "thread age:" in text and "CacheLIRS$Segment.put" in text
    count = build_chart(aemcs, mode="count")
    busiest = max(count.points, key=lambda p: p.value)
    assert f"{int(busiest.value)} thread(s)" in describe(busiest)


def test_html_export(aemcs):
    chart = build_chart(aemcs, mode="duration", group_by="pool", tz=UTC)
    page = to_html(chart, "test <title>")
    assert page.startswith("<!doctype html>") and "test &lt;title&gt;" in page
    assert 0 < page.count("<circle") <= len(chart.points)  # overlapping points share a marker
    assert page.count('class="key"') == len(chart.series)
    assert "prefers-color-scheme: dark" in page
    tips = json.loads(re.search(r"const tips = (.*);\n", page).group(1).replace("<\\/", "</"))
    assert len(tips) == page.count("<circle")
    assert sum(int(t.split(" threads here")[0]) if " threads here" in t else 1 for t in tips) == len(chart.points)


def test_html_export_escapes_thread_names(aemcs):
    dumps = load([str(aemcs[0].source)])
    dumps[0].threads[0].name = "</script><script>alert(1)</script>"
    page = to_html(build_chart(dumps, mode="duration"))
    assert page.count("</script>") == 1  # the name cannot close the script block
    assert "&lt;/script&gt;&lt;script&gt;alert(N)" in page  # legend entry is HTML-escaped


@pytest.mark.parametrize(
    ("name", "pool"),
    [
        ("qtp1996920089-84", "qtpN-N"),
        ("qtp1996920089-84-acceptor-0@4bd63113-ServerConnector@b308372{HTTP/1.1}", "qtpN-N-acceptor-N"),
        # Sling pools carry a random UUID (observed in real AEMaaCS dumps)
        (
            "sling-threadpool-0f1e2d3c-4b5a-4697-8877-665544332211-(apache-sling-job-thread-pool)-3",
            "sling-threadpool-*-(apache-sling-job-thread-pool)-N",
        ),
        (
            "sling-threadpool-a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d-(apache-sling-job-thread-pool)-12",
            "sling-threadpool-*-(apache-sling-job-thread-pool)-N",
        ),
        ("worker-5f3c9a7d21", "worker-*"),
        ("Feedback facade-7", "Feedback facade-N"),  # hex-looking words without digits stay
        ("GC Thread#0", "GC Thread#N"),
    ],
)
def test_pool_name(name, pool):
    assert pool_name(name) == pool
