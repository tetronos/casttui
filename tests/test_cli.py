"""Command line parsing."""
from __future__ import annotations

import pytest

from casttui.cli import parse_args, parse_source


def test_parse_source_none_url_and_file(tmp_path, monkeypatch):
    f = tmp_path / "a clip.mp4"
    f.write_bytes(b"x")
    assert parse_source([]) is None
    assert parse_source(["https://x/y.mp4"]) == "https://x/y.mp4"
    monkeypatch.chdir(tmp_path)
    assert parse_source(["a clip.mp4"]) == str(f)  # made absolute


def test_parse_source_rejects_a_missing_file_and_extra_args(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        parse_source([str(tmp_path / "nope.mp4")])
    assert e.value.code == 2 and "not a file" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        parse_source(["a", "b"])


def test_device_flag():
    args = parse_args(["-d", "192.168.1.50", "https://x/y"])
    assert (args.device, args.source) == ("192.168.1.50", "https://x/y")
    with pytest.raises(SystemExit):
        parse_args(["--classic"])  # the classic UI is gone


def test_help_lists_every_setting(capsys):
    from casttui.settings import SETTINGS

    with pytest.raises(SystemExit):
        parse_args(["--help"])
    out = capsys.readouterr().out
    for s in SETTINGS:
        assert s.key in out


def test_version(capsys):
    from casttui import __version__

    with pytest.raises(SystemExit):
        parse_args(["--version"])
    assert capsys.readouterr().out.strip() == f"casttui {__version__}"


def test_headless_flags_parse_and_conflict(capsys):
    a = parse_args(["--page", "http://10.0.0.5:8765/tok/", "-d", "TV"])
    assert (a.page, a.device, a.source) == ("http://10.0.0.5:8765/tok/", "TV", None)
    assert parse_args(["--stop"]).stop and parse_args(["--status"]).status
    assert parse_args(["--desktop", "-d", "TV"]).desktop
    for bad in (["--page", "http://x/", "https://y"], ["--stop", "--status"],
                ["--page", "file:///etc/passwd"], ["--stop", "https://y"],
                ["--desktop", "--stop"], ["--desktop", "x.mp4"]):
        with pytest.raises(SystemExit) as e:
            parse_args(bad)
        assert e.value.code == 2
