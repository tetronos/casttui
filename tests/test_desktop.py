"""Desktop casting: backend detection, ffmpeg commands, fMP4 splitting, the
token server (with the ffmpeg test pattern, skipped without ffmpeg), and the
headless loop with a fake service."""
from __future__ import annotations

import shutil
import struct
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from casttui import desktop as dt
from casttui import headless
from casttui.service import CastService, served_url


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def full(kind: bytes, flags: int, payload: bytes) -> bytes:
    return box(kind, struct.pack(">I", flags) + payload)


def moof(first_flags=None, default=None) -> bytes:
    tfhd = full(b"tfhd", 0x20 if default is not None else 0,
                struct.pack(">I", 1) + (struct.pack(">I", default) if default is not None else b""))
    if first_flags is not None:
        trun = full(b"trun", 0x5, struct.pack(">III", 1, 0, first_flags))
    else:
        trun = full(b"trun", 0x1, struct.pack(">II", 1, 0))
    return box(b"moof", box(b"mfhd", b"\0" * 8) + box(b"traf", tfhd + trun))


def test_detect_backend():
    no = lambda _: None  # noqa: E731
    assert dt.detect_backend({"DISPLAY": ":0"}, no, lambda _: False) == "x11"
    assert dt.detect_backend({"WAYLAND_DISPLAY": "w"}, lambda _: "/x", no) == "wayland"
    assert dt.detect_backend({}, no, lambda p: p == "/dev/fb0") == "fbdev"
    with pytest.raises(RuntimeError, match="wf-recorder"):
        dt.detect_backend({"WAYLAND_DISPLAY": "w", "DISPLAY": ":0"}, no, no)
    with pytest.raises(RuntimeError, match="no screen"):
        dt.detect_backend({}, no, lambda _: False)


def test_capture_cmd():
    x = dt.capture_cmd("x11", 30, 720, env={"DISPLAY": ":1"})
    assert x[:2] == ["ffmpeg", "-nostdin"] and "x11grab" in x and ":1" in x
    assert "scale=-2:720,format=yuv420p" in x and x[-1] == "pipe:1" and "pulse" not in x
    assert "zerolatency" in x and x[x.index("-g") + 1] == "30"
    a = dt.capture_cmd("fbdev", 15, 0, audio=True)
    assert "/dev/fb0" in a and "@DEFAULT_MONITOR@" in a and "aac" in a and "format=yuv420p" in a
    assert dt.capture_cmd("wayland")[0] == "sh"
    with pytest.raises(ValueError):
        dt.capture_cmd("vnc")


def test_moof_keyframe():
    assert dt.moof_keyframe(moof(first_flags=0x02000000))
    assert not dt.moof_keyframe(moof(first_flags=0x01010000))
    assert not dt.moof_keyframe(moof(default=0x01010000))
    assert dt.moof_keyframe(moof())


def test_splitter_any_byte_split():
    init = box(b"ftyp", b"isom") + box(b"moov", b"xxxxavcC\x01\x64\x00\x29")
    k, f = moof(first_flags=0x02000000), moof(first_flags=0x01010000)
    data = init + k + box(b"mdat", b"A") + f + box(b"mdat", b"B")
    for step in (1, 7, len(data)):
        sp, items = dt.Fmp4Splitter(), []
        for i in range(0, len(data), step):
            items += sp.feed(data[i:i + step])
        assert [i[0] for i in items] == ["init", "frag", "frag"]
        assert items[0][1] == init and items[1][2] is True and items[2][2] is False
    assert dt.codec_string(init) == "avc1.640029"
    assert dt.codec_string(b"", audio=True) == "avc1.640029,mp4a.40.2"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_server_with_test_pattern():
    s = dt.DesktopServer(dt.capture_cmd("test", 30, 360), "127.0.0.1")
    s.start()
    try:
        assert s.wait_ready(10) and len(s.token) == 32
        page = urllib.request.urlopen(s.url, timeout=5).read().decode()
        assert 'codecs="avc1.' in page and "muted" in page and "http" not in page.split("<script>")[1]
        r1 = urllib.request.urlopen(s.url + "stream", timeout=5)
        head = r1.read1(64)
        assert head[4:8] == b"ftyp"
        r2 = urllib.request.urlopen(s.url + "stream", timeout=5)
        with pytest.raises(urllib.error.HTTPError) as e:  # third viewer
            urllib.request.urlopen(s.url + "stream", timeout=5)
        assert e.value.code == 503
        assert s.viewers() == 2 and s.idle_seconds() == 0
        for bad in ("/", "/" + "0" * 32 + "/", f"/{s.token}/../"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"http://127.0.0.1:{s.port}{bad}", timeout=5)
            assert e.value.code == 404
        r1.close(), r2.close()
    finally:
        s.stop()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_server_refuses_other_addresses():
    s = dt.DesktopServer(dt.capture_cmd("test", 15, 240), "127.0.0.1", allow={"10.9.9.9"})
    s.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(s.url, timeout=5)
        assert e.value.code == 403
    finally:
        s.stop()


class FakeCast:
    def __init__(self):
        self.app_id = None


class FakeService(CastService):
    """The real cast_desktop and stop logic; only the device calls are fake."""

    def __init__(self):
        super().__init__()
        self.sites, self.stopped = [], False
        self.dev = SimpleNamespace(name="TV", ip_addr="127.0.0.1", _cast=FakeCast())

    def connect(self, target):
        return self.dev

    def _load_page(self, dev, url):
        self.sites.append(url)
        dev._cast.app_id = "84912283"

    def _cast(self, dev, url, ctype, title=None):
        self.sites.append(url)
        dev._cast.app_id = "CC1AD845"

    def stop(self):
        self.stopped = True
        self._stop_fileserver()


def cfg(**kw):
    base = dict(last_device="TV", desktop_backend="test", desktop_fps=15,
                desktop_scale=240, desktop_audio=False, desktop_receiver="dashcast")
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_headless_desktop_casts_then_ends_when_the_tv_moves_on(monkeypatch, capsys):
    monkeypatch.setattr("casttui.service.get_local_ip", lambda host: "127.0.0.1")
    monkeypatch.setattr(headless, "DESKTOP_GRACE", 0.0)
    s, lines = FakeService(), []

    def tv_switches_app():
        while not s.sites:
            time.sleep(0.05)
        time.sleep(1.5)
        s.dev._cast.app_id = "CC1AD845"  # Default Media Receiver took over

    threading.Thread(target=tv_switches_app, daemon=True).start()
    assert headless.desktop(config=cfg(), service=s, out=lines.append) == 0
    assert s.sites and s.sites[0].startswith("http://127.0.0.1:")
    token = s.sites[0].rstrip("/").rsplit("/", 1)[1]
    assert lines[0].startswith("CASTING DESKTOP (test) to TV")
    assert "no longer showing" in lines[-1]
    assert token not in "".join(lines) + capsys.readouterr().err


def test_headless_desktop_capture_failure_is_one_line(monkeypatch, capsys):
    monkeypatch.setattr("casttui.service.get_local_ip", lambda host: "127.0.0.1")
    monkeypatch.setattr("casttui.service.capture_cmd", lambda *a, **k: ["sh", "-c", "echo boom >&2; exit 1"])
    assert headless.desktop(config=cfg(), service=FakeService(), out=print) == 1
    assert "casttui: screen capture failed: boom" in capsys.readouterr().err


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_headless_desktop_media_receiver_gets_the_stream_url(monkeypatch):
    monkeypatch.setattr("casttui.service.get_local_ip", lambda host: "127.0.0.1")
    s, stop = FakeService(), threading.Event()
    threading.Timer(1.0, stop.set).start()
    assert headless.desktop(config=cfg(desktop_receiver="media"), service=s, stop=stop,
                            out=lambda _: None) == 0
    assert s.sites[0].endswith("/stream")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_service_holds_the_desktop_server_and_hides_its_url(monkeypatch):
    monkeypatch.setattr("casttui.service.get_local_ip", lambda host: "127.0.0.1")
    s = FakeService()
    s.device = s.dev
    server = s.cast_desktop("test", 15, 240)
    assert s.desktop() is server and server.backend == "test"
    assert served_url(s) is None  # the token URL is never shown
    s.stop()
    assert s.desktop() is None and server._proc.poll() is not None


def test_player_places_fragments_in_sequence_and_has_a_debug_line():
    """A late viewer is primed with a GOP stamped with the encoder's uptime; without
    sequence mode the clock waits at 0 in front of that data and the TV stays blank."""
    from casttui.desktop import PLAYER_HTML
    assert 'sb.mode = "sequence"' in PLAYER_HTML
    assert "?debug" in PLAYER_HTML
