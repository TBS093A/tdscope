import gzip
from datetime import datetime
from pathlib import Path

from tdscope import load, parse_lines
from tdscope.parser import parse_request, parse_thread_header


def test_jdk11_header():
    t = parse_thread_header(
        '"qtp1-2 [1790855995000] GET /a.html?x=1 HTTP/1.1" #101 daemon prio=5 os_prio=-2 cpu=1.50ms '
        "elapsed=12.25s tid=0x00007f2a10001000 nid=0x2b01 runnable  [0x00007f2a0f1fe000]"
    )
    assert t is not None
    assert t.name == "qtp1-2 [1790855995000] GET /a.html?x=1 HTTP/1.1"
    assert (t.number, t.daemon, t.prio, t.os_prio) == (101, True, 5, -2)
    assert (t.cpu_ms, t.elapsed_s) == (1.5, 12.25)
    assert (t.tid, t.nid, t.status) == ("0x00007f2a10001000", 0x2B01, "runnable")
    assert t.request is not None
    assert (t.request.method, t.request.path, t.request.started_at_ms) == ("GET", "/a.html?x=1", 1790855995000)


def test_jdk21_header_with_decimal_nid():
    t = parse_thread_header(
        '"worker-A" #31 [4101] prio=5 os_prio=0 cpu=12.50ms elapsed=30.00s tid=0x00007f0000001f nid=4101 '
        "waiting for monitor entry  [0x00007f0000aa0000]"
    )
    assert t is not None
    assert (t.number, t.nid, t.daemon, t.status) == (31, 4101, False, "waiting for monitor entry")


def test_vm_thread_header_without_number():
    t = parse_thread_header('"GC Thread#0" os_prio=0 cpu=4.00ms elapsed=1.00s tid=0x01 nid=0x1a01 runnable  ')
    assert t is not None
    assert (t.name, t.number, t.status) == ("GC Thread#0", None, "runnable")


def test_quotes_inside_thread_name():
    t = parse_thread_header('"say "hi" thread" #5 prio=5 os_prio=0 tid=0x01 nid=0x02 runnable')
    assert t is not None and t.name == 'say "hi" thread'


def test_deadlock_report_line_is_not_a_thread():
    assert parse_thread_header('"worker-A":') is None


def test_request_parsing():
    assert parse_request("pool-1-thread-3") is None
    r = parse_request("qtp-7 - DELETE /api/item/7 HTTP/2.0")
    assert r is not None and (r.method, r.path, r.protocol, r.started_at_ms) == (
        "DELETE",
        "/api/item/7",
        "HTTP/2.0",
        None,
    )


def test_request_with_client_ip_prefix():
    # AEM as a Cloud Service names request threads "<client ip> [<start>] <request line>"
    r = parse_request("192.0.2.10 [1714125143000] POST /libs/wcm/core/content/reference.json HTTP/1.1")
    assert r is not None
    assert (r.method, r.path, r.started_at_ms) == ("POST", "/libs/wcm/core/content/reference.json", 1714125143000)


def test_jetty_acceptor_is_not_a_request():
    assert parse_request("qtp1-83-acceptor-0@4bd6-ServerConnector@b308{HTTP/1.1, (http/1.1)}{0.0.0.0:8080}") is None


def test_jdk11_dump(fixtures):
    (dump,) = load([str(fixtures / "aem-node1-1.dump")])
    assert dump.timestamp == datetime(2026, 10, 1, 12, 0, 0)
    assert dump.vm_info == "OpenJDK 64-Bit Server VM (11.0.20+8 mixed mode)"
    names = [t.name for t in dump.threads]
    assert "VM Thread" in names and "GC Thread#0" in names
    assert len(dump.threads) == 8  # SMR info and synchronizer blocks are not threads

    stuck = next(t for t in dump.threads if t.name.startswith("qtp1001-101"))
    assert stuck.state == "RUNNABLE"
    assert stuck.frames[2].startswith("io.wcm.handler.url.impl.UrlHandlerImpl.externalize")
    assert [(i, e.kind, e.address) for i, e in stuck.locks] == [(2, "locked", "0x00000000c0ffee00")]

    owner = next(t for t in dump.threads if t.name == "batch-worker-1")
    assert (owner.state, owner.state_detail) == ("TIMED_WAITING", "sleeping")
    assert [e.address for e in owner.owned_synchronizers] == ["0x00000000c2000000"]

    blocked = next(t for t in dump.threads if t.name.startswith("qtp1001-102"))
    w = blocked.waiting_for()
    assert w is not None and (w.kind, w.class_name) == ("waiting to lock", "com.example.site.Cache")


def test_jdk21_deadlock_after_jni_line(fixtures):
    (dump,) = load([str(fixtures / "jdk21-deadlock.tdump")])
    assert [t.name for t in dump.threads] == ["worker-A", "worker-B", "VM Thread"]
    assert len(dump.deadlocks) == 1
    assert dump.deadlocks[0].threads == ["worker-A", "worker-B"]


def test_jdk8_dump_inside_crlf_log(fixtures):
    (dump,) = load([str(fixtures / "jdk8-app.log")])
    assert dump.timestamp == datetime(2026, 10, 1, 12, 0, 0)
    assert [t.name for t in dump.threads] == ["main", "Finalizer", "VM Thread"]
    finalizer = dump.threads[1]
    assert finalizer.cpu_ms is None and finalizer.nid == 0x4E25
    assert finalizer.state_detail == "on object monitor"
    assert finalizer.frames[-1] == "java.lang.ref.Finalizer$FinalizerThread.run(Finalizer.java:216)"


def test_multiple_dumps_in_one_input(fixtures):
    text = (
        (fixtures / "aem-node1-1.dump").read_text() + "\nsome log line\n" + (fixtures / "aem-node1-2.dump").read_text()
    )
    dumps = list(parse_lines(text.splitlines(), source="x"))
    assert [d.index for d in dumps] == [0, 1]
    assert [d.timestamp.second for d in dumps] == [0, 10]
    assert all(len(d.threads) == 8 for d in dumps)


def test_fragment_without_banner():
    lines = [
        '"t1" #1 prio=5 os_prio=0 tid=0x1 nid=0x1 runnable',
        "   java.lang.Thread.State: RUNNABLE",
        "\tat a.B.c(B.java:1)",
    ]
    (dump,) = parse_lines(lines)
    assert dump.timestamp is None and [t.frames for t in dump.threads] == [["a.B.c(B.java:1)"]]


def test_no_dump_in_plain_log(tmp_path, capsys):
    log = tmp_path / "app.log"
    log.write_text("nothing to see\n")
    assert load([str(log)]) == []
    assert "no thread dump found" in capsys.readouterr().err


def test_gzip_and_directory_scan(tmp_path, fixtures):
    data = (fixtures / "aem-node1-1.dump").read_bytes()
    (tmp_path / "nested").mkdir()
    with gzip.open(tmp_path / "nested" / "d.dump.gz", "wb") as fh:
        fh.write(data)
    (tmp_path / "ignored.bin").write_bytes(data)
    dumps = load([str(tmp_path)])
    assert len(dumps) == 1 and dumps[0].source.endswith("d.dump.gz")


def test_jvm_identity(all_dumps):
    ids = {Path(d.source).name: d.jvm_id for d in all_dumps}
    assert ids["aem-node1-1.dump"] == ids["aem-node1-2.dump"] == ids["aem-node1-3.dump"]
    assert len(set(ids.values())) == 3
