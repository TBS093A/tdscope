#!/usr/bin/env python3
"""Smoke test of an *installed* tdscope (built wheel or a release from PyPI).

    python scripts/smoke_test.py [--version X.Y.Z] [--tui]

Runs from a temporary directory, so the package under test is the installed one,
never the source tree. Uses the synthetic fixtures of this repository as input.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def run(*args: str) -> str:
    print("$ tdscope", " ".join(args), flush=True)
    done = subprocess.run(["tdscope", *args], capture_output=True, text=True, encoding="utf-8", check=False)
    if done.returncode != 0:
        print(done.stdout, done.stderr, sep="\n")
        raise SystemExit(f"tdscope {' '.join(args)} exited with {done.returncode}")
    return done.stdout


async def drive_tui(folder: Path) -> None:
    from tdscope.tui import TdscopeApp

    app = TdscopeApp(str(folder))
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(200):
            if app.last is not None:
                break
            await pilot.pause(0.05)
        assert app.last is not None, "TUI did not load the dumps"
        await pilot.press("g")
        await pilot.pause(0.3)
        assert type(app.screen).__name__ == "ChartScreen", "timeline graph did not open"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="expected installed version")
    parser.add_argument("--tui", action="store_true", help="also drive the TUI headlessly")
    opts = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="tdscope-smoke-"))
    data = work / "dumps"
    shutil.copytree(FIXTURES / "aemcs", data)
    os.chdir(work)
    sys.path = [p for p in sys.path if not p or Path(p).resolve() != FIXTURES.parents[1] / "src"]

    import tdscope

    location = Path(tdscope.__file__).resolve()
    print(f"tdscope {tdscope.__version__} from {location}")
    assert "site-packages" in location.parts, "tdscope is not imported from an installed distribution"
    if opts.version:
        assert tdscope.__version__ == opts.version, f"installed {tdscope.__version__}, expected {opts.version}"
    assert run("--version").strip() == f"tdscope {tdscope.__version__}"

    summary = json.loads(run("summary", str(data), "-f", "json", "-q"))
    assert len(summary) == 7, summary
    requests = json.loads(run("requests", str(data), "--tz", "UTC", "-f", "json", "-q"))
    assert requests[0]["key"] == "POST /libs/wcm/core/content/reference.json", requests[0]
    locks = json.loads(run("locks", str(data), "-f", "json", "-q"))
    assert len(locks["contentions"]) == 2, locks
    assert "io.wcm." in run("frames", str(data), "-m", "io.wcm.", "-q")
    report = work / "out" / "stuck.txt"
    report.parent.mkdir()
    run("stuck", str(data), "-q", "-o", str(report))
    assert "GET /sites.html/content/example/en" in report.read_text(encoding="utf-8")

    if opts.tui:
        assert "PATH" in run("tui", "--help")
        asyncio.run(drive_tui(data))
        print("TUI: loaded dumps and opened the timeline graph")

    print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
