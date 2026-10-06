"""The parser must account for every thread, frame, state and lock line of every fixture."""

from pathlib import Path

import pytest
from rawcount import RawCounts, count

from tdscope import parse_lines
from tdscope.loader import discover

FIXTURES = Path(__file__).parent / "fixtures"
ALL_FIXTURES = discover([str(FIXTURES)])


def test_fixture_set_is_not_empty():
    assert len(ALL_FIXTURES) >= 10


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_parser_matches_raw_text(path: Path):
    text = path.read_bytes().decode("utf-8")
    dumps = list(parse_lines(text.splitlines(), source=str(path)))
    threads = [t for d in dumps for t in d.threads]
    parsed = RawCounts(
        dumps=len(dumps),
        threads=len(threads),
        frames=sum(len(t.frames) for t in threads),
        states=sum(1 for t in threads if t.state),
        lock_lines=sum(len(t.locks) for t in threads),
    )
    assert parsed == count(text)


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_every_thread_header_is_fully_parsed(path: Path):
    for dump in parse_lines(path.read_bytes().decode("utf-8").splitlines()):
        assert dump.timestamp is not None
        for t in dump.threads:
            assert t.tid and t.tid.startswith("0x"), t.header
            assert t.nid is not None and t.status, t.header
            if t.number is not None:  # Java threads carry a priority, VM threads do not
                assert t.prio is not None, t.header
            if t.frames:
                assert t.state is not None, t.header
