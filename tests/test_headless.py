"""Headless commands with a fake service: no device, no network."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from casttui import headless

TOKEN_URL = "http://10.0.0.5:8765/9f3c2b7a1d/view?k=secret"


class FakeService:
    def __init__(self, found=(), fail=None, hang=False):
        self.found, self.fail, self.hang = list(found), fail, hang
        self.calls, self.device, self.closed = [], None, False

    def connect(self, target):
        self.calls.append(("connect", target))
        return SimpleNamespace(name=target)

    def discover(self):
        self.calls.append(("discover",))
        return self.found

    def cast_site(self, url):
        if self.hang:
            threading.Event().wait()
        if self.fail:
            raise RuntimeError(self.fail)
        self.calls.append(("cast_site", url))

    def stop(self):
        self.calls.append(("stop",))

    def close(self):
        self.closed = True


def cfg(last=None):
    return SimpleNamespace(last_device=last)


def test_page_casts_and_hides_the_token(capsys):
    s = FakeService()
    assert headless.run("page", url=TOKEN_URL, device="TV", config=cfg(), service=s) == 0
    assert s.calls == [("connect", "TV"), ("cast_site", TOKEN_URL)] and s.closed
    out = capsys.readouterr()
    assert "http://10.0.0.5:8765" in out.out
    assert "9f3c" not in out.out + out.err and "secret" not in out.out + out.err


def test_error_line_never_carries_the_token(capsys):
    s = FakeService(fail=f"could not load {TOKEN_URL}")
    assert headless.run("page", url=TOKEN_URL, device="TV", config=cfg(), service=s) == 1
    err = capsys.readouterr().err
    assert err.startswith("casttui: ") and "9f3c" not in err and "10.0.0.5:8765" in err


def test_device_resolution_order():
    s = FakeService()
    headless.run("stop", config=cfg("Living Room TV"), service=s)
    assert s.calls[0] == ("connect", "Living Room TV")
    one = SimpleNamespace(name="Only TV")
    s = FakeService(found=[one])
    assert headless.run("stop", config=cfg(), service=s) == 0
    assert s.calls == [("discover",), ("stop",)] and s.device is one


@pytest.mark.parametrize("found,msg", [([], "no Chromecast found"),
                                       ([SimpleNamespace(name="A"), SimpleNamespace(name="B")],
                                        "several Chromecasts found (A, B)")])
def test_no_or_several_devices_is_an_error(found, msg, capsys):
    assert headless.run("stop", config=cfg(), service=FakeService(found=found)) == 1
    assert msg in capsys.readouterr().err


def test_timeout(capsys):
    t0 = time.monotonic()
    s = FakeService(hang=True)
    assert headless.run("page", url=TOKEN_URL, device="TV", config=cfg(), service=s,
                        timeout=0.2) == 1
    assert time.monotonic() - t0 < 2
    assert "no answer within" in capsys.readouterr().err


def test_status_line():
    def dev(app, idle=False, state=None):
        mc = SimpleNamespace(status=SimpleNamespace(player_state=state))
        cast = SimpleNamespace(app_display_name=app, is_idle=idle, media_controller=mc)
        return SimpleNamespace(name="TV", _cast=cast)

    assert headless.status_line(dev(None, idle=True)) == "TV: idle"
    assert headless.status_line(dev("Backdrop")) == "TV: idle"
    assert headless.status_line(dev("DashCast")) == "TV: dashcast"
    assert headless.status_line(dev("Default Media Receiver", state="PLAYING")) == \
        "TV: default media receiver playing"


def test_safe_url():
    assert headless.safe_url(TOKEN_URL) == "http://10.0.0.5:8765"
