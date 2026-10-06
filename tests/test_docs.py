"""The documentation's demo data and screenshot tooling keep working."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from rawcount import count

from tdscope import load
from tdscope.analysis import by_jvm

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_demo_dumps(tmp_path):
    sys.path.insert(0, str(DOCS / "demo"))
    try:
        from make_demo_dumps import DUMPS, main
    finally:
        sys.path.remove(str(DOCS / "demo"))
    main(tmp_path)
    dumps = load([str(tmp_path)])
    assert len(dumps) == 2 * DUMPS and len(by_jvm(dumps)) == 2
    for path in tmp_path.rglob("*.dump"):  # parser accounts for everything in the demo files
        raw = count(path.read_text(encoding="utf-8"))
        assert raw.threads == sum(len(d.threads) for d in dumps if d.source == str(path))


def test_screenshots_referenced_by_the_guide_exist_and_are_svg():
    guide = (DOCS / "TUI.md").read_text(encoding="utf-8") + (DOCS.parent / "README.md").read_text(encoding="utf-8")
    names = {line.split("screenshots/")[1].split(")")[0] for line in guide.splitlines() if "screenshots/" in line}
    names.discard("`.")  # prose mentioning the folder
    assert len(names) >= 10
    for name in names:
        svg = DOCS / "screenshots" / name
        assert svg.exists(), name
        assert ET.parse(svg).getroot().tag.endswith("svg")
