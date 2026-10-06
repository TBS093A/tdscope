import json

import pytest

from tdscope.cli import main


def test_json_output(fixtures, capsys):
    assert main(["requests", str(fixtures), "-m", "io.wcm.", "--tz", "UTC", "-f", "json", "-q"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["key"] == "GET /content/site/en.html"
    assert data[0]["request_age_s"]["max"] == 25.0


def test_output_file_and_top(fixtures, tmp_path, capsys):
    out = tmp_path / "report.txt"
    assert main(["frames", str(fixtures), "-m", "io.wcm.", "-t", "1", "-o", str(out)]) == 0
    lines = out.read_text().splitlines()
    assert len(lines) == 2 and "LinkHandlerImpl" in lines[1]
    assert "dump(s)" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["summary", "requests", "hotspots", "stuck", "locks", "cpu"])
def test_every_command_runs(fixtures, command, capsys):
    assert main([command, str(fixtures), "--stack", "-q"]) == 0
    assert capsys.readouterr().out


def test_frames_without_match_is_a_usage_error(fixtures):
    with pytest.raises(SystemExit) as exc:
        main(["frames", str(fixtures)])
    assert exc.value.code == 2


def test_missing_path(capsys):
    assert main(["summary", "/does/not/exist"]) == 2


def test_no_dumps(tmp_path):
    (tmp_path / "a.log").write_text("hello\n")
    assert main(["summary", str(tmp_path), "-q"]) == 1


def test_invalid_timezone(fixtures):
    with pytest.raises(SystemExit):
        main(["requests", str(fixtures), "--tz", "Mars/Olympus"])
