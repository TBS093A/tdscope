"""DAST for the only web-facing artifact: the exported HTML chart, opened in a real browser.

Thread names come from the analysed JVM, i.e. from untrusted input. Opening an export
must never execute injected script and must never contact the network.
Needs ``pip install playwright && playwright install chromium``; skipped otherwise.
"""

import pytest

from tdscope.parser import parse_lines
from tdscope.timeline import build_chart, to_html

pytestmark = pytest.mark.dast
playwright_api = pytest.importorskip("playwright.sync_api")

PAYLOADS = [
    "</script><script>alert('script')</script>",
    "<img src=x onerror=alert('img')>",
    '"><svg onload=alert("svg")>',
    "'); alert('js'); ('",
    "${alert('template')}",
    "<a href=javascript:alert('href')>x</a>",
    "<iframe src=https://example.com/></iframe>",
    "<link rel=stylesheet href=https://example.com/x.css>",
    chr(0x2028) + chr(0x2029) + " line separators",  # break naive JS string escaping
]


def _dumps():
    lines = []
    for minute in range(3):
        lines += [f"2026-01-01 10:0{minute}:00", "Full thread dump OpenJDK (21):", ""]
        for i, payload in enumerate(PAYLOADS):
            lines += [
                f'"{payload}" #{i} prio=5 os_prio=0 cpu=1.00ms elapsed={60 + minute}.00s '
                f"tid=0x{i + 1:x} nid=0x{i:x} runnable",
                "   java.lang.Thread.State: RUNNABLE",
                f"\tat com.example.{i}.Evil.run({payload})",
                "",
            ]
        lines.append("JNI global refs: 1, weak refs: 0")
    return list(parse_lines(lines))


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser binaries not installed
            pytest.skip(f"chromium not available: {exc}")
        yield browser
        browser.close()


@pytest.mark.parametrize(("mode", "group_by"), [("count", "pool"), ("duration", "pool"), ("count", "code")])
def test_exported_chart_is_inert(browser, tmp_path, mode, group_by):
    page_file = tmp_path / "chart.html"
    page_file.write_text(to_html(build_chart(_dumps(), mode=mode, group_by=group_by, max_groups=4), "<b>title</b>"))

    page = browser.new_page()
    dialogs, requests, errors = [], [], []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.on("request", lambda r: requests.append(r.url) if not r.url.startswith(("file:", "data:")) else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(page_file.as_uri())

    circles = page.locator("circle")
    assert circles.count() > 0
    for i in range(min(circles.count(), 40)):  # hovering renders every tooltip
        circles.nth(i).hover(force=True)
    tip = page.locator("#tip").text_content() or ""
    for key in page.locator("button.key").all():  # legend toggles still work
        key.click()

    assert dialogs == [], "injected script ran"
    assert requests == [], "the page contacted the network"
    assert errors == []
    assert page.locator("img, iframe, link[rel=stylesheet], a[href^=javascript]").count() == 0
    assert page.locator("script").count() == 1
    assert tip  # tooltips show the hostile names as plain text
    page.close()
