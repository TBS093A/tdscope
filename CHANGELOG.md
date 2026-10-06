# Changelog

## 0.1.0 - unreleased

First public release.

- Streaming parser for HotSpot thread dumps (JDK 8-21+, jstack / jcmd / kill -3,
  dumps embedded in logs, gzip, CRLF, JVM deadlock reports).
- Analyses: `summary`, `requests`, `frames`, `hotspots`, `stuck`, `locks`, `cpu`.
- Text and JSON output.
- `tdscope tui`: k9s-style terminal UI (optional `tui` extra) with forms for every
  analysis, a command line, export to text/JSON and a timeline graph (thread count or
  duration over time, colored by group, hover details, HTML export). Groupings by pool,
  family, code (package of the executing frame), state or request; the "other" group
  is broken down in the legend and on hover and can be expanded into its own chart.

### Security and release engineering

- Fixed a regular expression denial of service: a crafted lock line in a dump made
  parsing take minutes (quadratic backtracking). Found by the new hostile-input tests.
- Pre-merge quality gate: gitleaks, CodeQL and bandit (SAST), pip-audit and dependency
  review (SCA), fuzzing and a browser test of the HTML export (dynamic testing), tests on
  Linux, macOS and Windows.
- Releases from `vX.Y.Z` tags to PyPI via trusted publishing, with verification of the
  built and the published package; development builds go to TestPyPI after each merge.
- pre-commit hooks: gitleaks, ruff, no real thread dumps, no private words.
