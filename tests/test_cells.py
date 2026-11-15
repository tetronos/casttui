"""--cells: the grid server, its HTTP rules, and the headless command with a fake device."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

from casttui import cells_page, headless
from casttui.cells import MAX_VIEWERS, CellsServer

HELLO = {"t": "hello", "v": 1, "cols": 4, "rows": 2}
WHITE, BLACK = 0xFFFFFFFF, 0x000000FF


@pytest.fixture
def server():
    s = CellsServer("127.0.0.1")
    s.start()
    yield s
    s.stop()


def get(server, path, **kw):
    c = http.client.HTTPConnection(server.address, server.port, timeout=3)
    c.request("GET", path, **kw)
    return c, c.getresponse()


def read_events(resp, n):
    out = []
    while len(out) < n:
        line = resp.fp.readline().decode()
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def test_page_is_the_recorded_copy():
    assert hashlib.sha256(cells_page.PAGE.encode()).hexdigest() == cells_page.PAGE_SHA256


def test_page_and_wrong_paths(server):
    c, r = get(server, f"/{server.token}/")
    assert r.status == 200 and b"EventSource" in r.read()
    for bad in ("/", f"/{server.token}", f"/{server.token}/other", "/wrong/"):
        _, r = get(server, bad)
        assert r.status == 404, bad
    c = http.client.HTTPConnection(server.address, server.port, timeout=3)
    c.request("POST", f"/{server.token}/")
    assert c.getresponse().status in (404, 501)


def test_allow_list_refuses_other_peers():
    s = CellsServer("127.0.0.1", allow={"10.9.9.9"})
    s.start()
    try:
        _, r = get(s, f"/{s.token}/")
        assert r.status == 403
    finally:
        s.stop()


def test_late_viewer_is_primed_with_the_current_grid(server):
    server.feed(HELLO)
    server.feed({"t": "full", "spans": [[0, 0, WHITE, BLACK, 0, "ab"], [0, 1, WHITE, BLACK, 1, "cd"]]})
    server.feed({"t": "diff", "spans": [[1, 0, WHITE, BLACK, 0, "Z"]]})
    server.feed({"t": "cursor", "x": 2, "y": 1})
    _, r = get(server, f"/{server.token}/events")
    hello, full, cursor = read_events(r, 3)
    assert hello == {"t": "hello", "v": 1, "cols": 4, "rows": 2}
    assert full["spans"] == [[0, 0, WHITE, BLACK, 0, "aZ"], [0, 1, WHITE, BLACK, 1, "cd"]]
    assert cursor == {"t": "cursor", "x": 2, "y": 1}


def test_live_events_reach_a_connected_viewer_and_stop_says_so(server):
    server.feed(HELLO)
    _, r = get(server, f"/{server.token}/events")
    read_events(r, 3)
    server.feed({"t": "diff", "spans": [[3, 1, WHITE, BLACK, 0, "!"]]})
    assert read_events(r, 1)[0]["spans"] == [[3, 1, WHITE, BLACK, 0, "!"]]
    server.stop()
    assert read_events(r, 1)[0] == {"t": "status", "state": "stopped"}


def test_bad_and_unknown_events_are_ignored(server):
    server.feed({"t": "diff", "spans": [[0, 0, 1, 2, 0, "x"]]})        # before hello
    server.feed({"t": "hello", "cols": 0, "rows": 5})                  # absurd size
    server.feed({"t": "nonsense"})
    server.feed(HELLO)
    server.feed({"t": "full", "spans": ["bad", [9, 9, 1, 2, 0, "x"], [0, 5, 1, 2, 0, "x"]]})
    assert server.cols == 4 and all(c is None for c in server._grid)


def test_viewer_cap(server):
    server.feed(HELLO)
    held = [get(server, f"/{server.token}/events") for _ in range(MAX_VIEWERS)]
    for _, r in held:
        assert r.status == 200
    t0 = time.monotonic()
    while server.viewers() < MAX_VIEWERS and time.monotonic() - t0 < 3:
        time.sleep(0.02)
    _, r = get(server, f"/{server.token}/events")
    assert r.status == 503


# -- headless.cells with a fake device ---------------------------------------------

class FakeCells:
    def __init__(self):
        self.calls, self.server, self.closed, self.device = [], None, False, None

    def connect(self, target):
        self.calls.append(("connect", target))
        return SimpleNamespace(name=target, _cast=SimpleNamespace(app_id="X"))

    def discover(self):
        return []

    def cells_server(self):
        self.server = CellsServer("127.0.0.1")
        self.server.start()
        return self.server

    def show_page(self, server):
        self.calls.append(("show_page", server.url))

    def stop(self):
        self.calls.append(("stop",))

    def close(self):
        self.closed = True
        if self.server:
            self.server.stop()


CFG = SimpleNamespace(last_device="TV")


def test_cells_command_feeds_the_server_and_quits_the_tv_when_the_pipe_closes(capsys):
    svc, lines = FakeCells(), []
    stdin = io.StringIO("\n".join(json.dumps(e) for e in [
        HELLO, {"t": "full", "spans": [[0, 0, WHITE, BLACK, 0, "hi"]]}]) + "\nnot json\n")
    code = headless.cells(config=CFG, service=svc, stdin=stdin, out=lines.append)
    assert code == 0
    assert ("show_page", svc.server.url) in svc.calls and ("stop",) in svc.calls
    assert lines == ["casting cells on TV", "stopped"]
    assert svc.server._grid[0][3] == "h" and svc.closed
    assert svc.server.url not in "".join(lines) + capsys.readouterr().err


def test_cells_command_reports_a_failed_cast_without_the_url(capsys):
    svc = FakeCells()
    svc.show_page = lambda server: (_ for _ in ()).throw(RuntimeError(f"cannot load {server.url}"))
    code = headless.cells(config=CFG, service=svc, stdin=io.StringIO(""), out=lambda m: None)
    err = capsys.readouterr().err
    assert code == 1 and "cannot load" in err and svc.server.token not in err and svc.closed
