from datetime import timezone
from pathlib import Path

import pytest

from tdscope import ThreadFilter, load
from tdscope import analysis as a
from tdscope.views import to_table

AEMCS = Path(__file__).parent / "fixtures" / "aemcs"


@pytest.fixture(scope="module")
def dumps():
    return load([str(AEMCS)])


RESULTS = {
    "summary": lambda d: a.summary(d, ThreadFilter()),
    "requests": lambda d: a.requests(d, ThreadFilter(), tz=timezone.utc),
    "frames": lambda d: a.frames(d, ThreadFilter(frame_patterns=["oak"])),
    "hotspots": lambda d: a.hotspots(d, ThreadFilter()),
    "stuck": lambda d: a.stuck(d, ThreadFilter()),
    "locks": lambda d: a.locks(d, ThreadFilter()),
    "cpu": lambda d: a.cpu(d, ThreadFilter()),
}


@pytest.mark.parametrize("command", RESULTS)
def test_every_result_becomes_a_table(dumps, command):
    table = to_table(command, RESULTS[command](dumps), root=str(AEMCS))
    assert table.rows, command
    assert len(table.rows) == len(table.details)
    assert all(len(row) == len(table.columns) for row in table.rows)
    assert not any(str(AEMCS) in cell for row in table.rows for cell in row)
    assert not any(str(AEMCS) in d for d in table.details)
    assert all(d.strip() for d in table.details)


def test_request_table(dumps):
    table = to_table("requests", RESULTS["requests"](dumps))
    row = table.rows[0]
    assert row[:4] == ["POST /libs/wcm/core/content/reference.json", "12", "2", "12"]
    assert "seen 12x" in table.details[0] and "\tat " in table.details[0]


def test_lock_table_lists_owner(dumps):
    table = to_table("locks", RESULTS["locks"](dumps))
    assert table.rows[0][0] == "org.apache.jackrabbit.oak.cache.CacheLIRS$Segment"
    assert table.rows[0][2] == "7" and "GET /sites.html" in table.rows[0][3]
