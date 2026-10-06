# tdscope

Command-line analyzer for Java (HotSpot) thread dumps. Point it at a directory of
`jstack` / `jcmd Thread.print` / `kill -3` output and it tells you:

- **which HTTP requests** were in flight, how old they were and where in *your* code they spent time,
- **which stacks dominate** (hotspots), grouped across all dumps,
- **which threads are stuck**: same stack in N consecutive dumps,
- **who blocks whom**: lock owners, their waiters and JVM-detected deadlocks,
- **who burns CPU** between two dumps.

The CLI has no runtime dependencies (Python 3.10+ standard library only), so it also
runs on a locked-down production box. An optional [interactive TUI](#interactive-tui)
with a timeline graph needs [Textual](https://textual.textualize.io/).

## Installation

```bash
pip install tdscope        # or: pipx install tdscope
```

From source:

```bash
git clone https://github.com/TBS093A/tdscope && cd tdscope
pip install -e ".[dev]"
```

## Taking thread dumps

A single dump is a snapshot; most problems only show up in a **series**. Take several
dumps a few seconds apart:

```bash
PID=$(pgrep -f my-app.jar)
for i in $(seq 1 6); do jcmd "$PID" Thread.print -l > "dump-$(date +%H%M%S).tdump"; sleep 10; done
```

`jstack -l <pid>` works the same way. Output of `kill -3` written to a log file
(e.g. `stdout.log`, `catalina.out`) can be fed in directly: tdscope finds the dumps
inside the log and ignores everything else. Gzipped files (`*.gz`) are read transparently.

## Usage

```
tdscope ANALYSIS [options] PATH [PATH ...]
```

`PATH` is a file or a directory (scanned recursively for `*.dump *.tdump *.txt *.log
*.out *.jstack *.gz`, change with `--glob`), or `-` for stdin.

| Analysis   | What it answers |
|------------|-----------------|
| `summary`  | thread count, states, HTTP requests and deadlocks per dump |
| `requests` | HTTP request threads grouped by `METHOD path`: count, request age, observed span, top frames |
| `frames`   | unique frames matching `--match` with counts (where does *my* code show up?) |
| `hotspots` | threads grouped by identical top N frames |
| `stuck`    | threads with an unchanged stack across `--min-dumps` consecutive dumps |
| `locks`    | contended monitors / `java.util.concurrent` locks with owner and waiters, deadlocks |
| `cpu`      | CPU consumed by each thread between consecutive dumps (JDK 11+) |

Options shared by all analyses:

| Option | Meaning |
|--------|---------|
| `-m, --match PATTERN` | keep threads having a frame that contains `PATTERN` (repeatable), e.g. `-m com.mycompany.` |
| `-E, --regex` | treat `--match` patterns as regular expressions |
| `-s, --state STATE` | keep threads in a `java.lang.Thread.State`, e.g. `-s BLOCKED` (repeatable) |
| `-n, --name REGEX` | keep threads whose name matches |
| `--http-only` | keep only threads serving an HTTP request |
| `-t, --top N` | show only the first N results |
| `--stack` | print an example stack for every result |
| `--max-stack-lines N` | truncate printed stacks |
| `-f json` | machine-readable output |
| `-o FILE` | write the report to a file |

### Examples

What were the HTTP requests doing in my code (`io.wcm.` here, a common AEM library)?

```bash
tdscope requests dumps/ -m io.wcm. --tz Europe/Warsaw
```

```
GET /content/site/en.html
  seen 3x in 3 dump(s), 1 distinct request(s)
  request age:   avg 15.0s (min 5.0s, max 25.0s)
  observed span: 20.0s
  thread age:    avg 1200.5s (min 1200.5s, max 1200.5s)   (elapsed=, age of the pooled thread)
  thread cpu:    avg 9000.0ms (min 1000.0ms, max 17000.0ms)
  states:        RUNNABLE=3
  frames:
        3x  io.wcm.handler.url.impl.UrlHandlerImpl.externalize(UrlHandlerImpl.java:120)
```

Which of my frames appear most often, and with what full stack?

```bash
tdscope frames dumps/ -m com.mycompany. --top-only --stack --max-stack-lines 40
```

Threads stuck for at least 4 dumps, as JSON:

```bash
tdscope stuck dumps/ --min-dumps 4 -f json -o stuck.json
```

Who holds the lock everybody is waiting for?

```bash
tdscope locks dumps/ -t 5
```

## Interactive TUI

```bash
pip install 'tdscope[tui]'
tdscope tui dumps/ --tz UTC
```

A [k9s](https://k9scli.io/)-style terminal UI on top of the same analyses. One key per
analysis opens a form with its options. Results appear in a table with the full report
of the selected row. A `:` command line takes CLI arguments, and results export to text
or JSON. The **timeline graph** plots threads over time (count per group, or the
duration of every thread), colored by pool, family, code, state or request, with hover
details and a self-contained HTML export.

![tdscope TUI timeline graph](docs/screenshots/07-timeline-count.svg)

**[TUI guide with screenshots and all keys](docs/TUI.md)**

## How to read the numbers

- **`elapsed=` is the age of the thread, not of the request.** Servlet containers reuse
  pooled threads, so a request thread may be hours old while serving a 50 ms request.
  tdscope reports it as *thread age* and never uses it as a request duration.
- **Request age** is computed only when the container writes the request start time
  (epoch millis) into the thread name, as Apache Sling / AEM does, e.g.
  `qtp123-45 [1700000000000] GET /content/page.html HTTP/1.1` or, on AEM as a Cloud
  Service, `<client ip> [1700000000000] POST /path HTTP/1.1`.
  Dump timestamps are written in the JVM's local time without a zone, so pass the JVM's
  zone with `--tz` when it differs from the machine running tdscope. If the dump time
  appears to precede the request start, tdscope flags it and suggests `--tz`; a zone
  that is wrong in the other direction cannot be detected and makes ages too long.
- **Observed span** needs no clock at all: it is the time between the first and the last
  dump in which the very same request (same thread, same name) was seen.
- **Cross-dump analyses** (`stuck`, `cpu`, observed span) compare dumps of the same JVM
  process only. The JVM is recognised by the native address of start-up threads such as
  "Reference Handler", so dumps of several nodes can be analysed together.
- `stuck` ignores idle threads by default: it considers RUNNABLE/BLOCKED threads and
  threads serving a request, minus threads "running" in well-known idle native frames
  (selectors, `accept()`, ...). Background threads blocked in a socket read (HTTP/2
  connection readers, long polling) are skipped as well; add `--include-network-wait`
  to hunt for outbound calls without a read timeout. Request threads are always kept.

## Supported formats

HotSpot / OpenJDK thread dumps from JDK 8 to JDK 21+: `jstack`, `jstack -l`,
`jcmd <pid> Thread.print [-l]` and `kill -3` / `SIGQUIT` output, including the JDK 19+
header format (`#1 [12345] ... nid=12345`), `Locked ownable synchronizers` sections,
JVM deadlock reports and CRLF line endings. Virtual-thread dumps
(`jcmd Thread.dump_to_file`) and IBM/OpenJ9 javacores are not supported yet.

## Using it as a library

```python
from tdscope import ThreadFilter, load
from tdscope import analysis

dumps = load(["dumps/"])
for group in analysis.hotspots(dumps, ThreadFilter(states=["BLOCKED"]), depth=8)[:3]:
    print(group.snapshots, group.signature[0])
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
mypy
```

Test fixtures in `tests/fixtures/` are synthetic. Please never commit real thread
dumps: they contain host names, URLs and sometimes user data.

## License

[MIT](LICENSE)
