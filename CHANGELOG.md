# Changelog

## 0.1.0 - unreleased

First public release.

- Streaming parser for HotSpot thread dumps (JDK 8-21+, jstack / jcmd / kill -3,
  dumps embedded in logs, gzip, CRLF, JVM deadlock reports).
- Analyses: `summary`, `requests`, `frames`, `hotspots`, `stuck`, `locks`, `cpu`.
- Text and JSON output.
- `tdscope tui`: k9s-style terminal UI (optional `tui` extra) with forms for every
  analysis, a command line, export to text/JSON and a timeline graph (thread count or
  duration over time, colored by group, hover details, HTML export).
