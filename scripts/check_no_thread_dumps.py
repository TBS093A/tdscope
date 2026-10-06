#!/usr/bin/env python3
"""pre-commit hook: refuse real thread dumps.

Thread dumps carry host names, URLs, client IPs and sometimes user data. Only the
synthetic fixtures under tests/fixtures/ may contain them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ALLOWED = ("tests/fixtures/",)
SIGNATURES = (
    re.compile(rb"^Full thread dump ", re.MULTILINE),
    re.compile(rb'^"[^"\n]*" #\d+ .*\btid=0x[0-9a-f]+ nid=', re.MULTILINE),
)


def main(paths: list[str]) -> int:
    bad = []
    for name in paths:
        if name.replace("\\", "/").startswith(ALLOWED):
            continue
        try:
            data = Path(name).read_bytes()
        except OSError:
            continue
        if any(sig.search(data) for sig in SIGNATURES):
            bad.append(name)
    for name in bad:
        print(f"{name}: looks like a real thread dump; keep dumps out of git (synthetic ones go to tests/fixtures/)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
