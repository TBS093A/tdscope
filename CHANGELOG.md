# Changelog

## 0.1.0 - 2026-10-06

First public release.

### Analysis

- Streaming parser for HotSpot thread dumps (JDK 8-21+, jstack / jcmd / kill -3,
  dumps embedded in logs, gzip, CRLF, JVM deadlock reports).
- Analyses: `summary`, `requests`, `frames`, `hotspots`, `stuck`, `locks`, `cpu`.
  Dumps of several JVMs (nodes, restarts) can be analysed together.
- Text and JSON output.

### Terminal UI

- `tdscope tui`: k9s-style terminal UI (optional `tui` extra) with forms for every
  analysis, a command line, and export to text/JSON.
- Timeline graph of threads over time (thread count or per-thread duration), colored by
  pool, family, code (package of the executing frame), state or request, with hover
  details and a self-contained HTML export. The "other" group is broken down in the
  legend and on hover and can be expanded into its own chart.
- Guide with screenshots: [docs/TUI.md](docs/TUI.md).

### Platforms

- Python 3.10-3.14 on Linux, macOS and Windows. No runtime dependencies, except
  `tzdata` on Windows.

### Security and release engineering

- The parser is hardened against hostile input: fuzzing and time-bounded tests guard
  against crashes and regular expression denial of service.
- Exported HTML is tested in a real browser: thread names from a dump cannot inject
  script, and the page makes no network requests.
- Pre-merge quality gate: gitleaks, CodeQL and bandit (SAST), pip-audit and dependency
  review (SCA), dynamic tests, and tests and package installation on Linux, macOS and
  Windows.
- Releases from `vX.Y.Z` tags to PyPI via trusted publishing, with verification of the
  built and the published package. Development builds go to TestPyPI after each merge.
- pre-commit hooks: gitleaks, ruff, no real thread dumps, no private words.
