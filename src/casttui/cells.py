"""Serve a tetron-wm cell grid to a page, for ``casttui --cells``.

tetron-wm pipes its desktop to casttui as one JSON object per line (protocol
v1, documented in docs/): ``hello`` (cols, rows), ``full`` and ``diff``
(``spans``: ``[x, y, fg, bg, attrs, text]``, colours 0xRRGGBBAA, attrs bits
bold 1, italic 2, underline 4, inverse 8), ``cursor`` (x, y or null) and
``status`` (state). Unknown fields and event types are ignored.

The server keeps the current grid, so a viewer that connects late (or
reconnects) is primed with a fresh ``full`` and then gets live events. It
serves GET only, under a secret path, to allowed peers, at most MAX_VIEWERS at
once. The token and the full URL are never logged or printed (except by
``--no-cast``, whose whole point is to hand the URL to the user).
"""
from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Iterable, List, Optional

from .cells_page import PAGE
from .fileserver import _ThreadingTCPServer

MAX_VIEWERS = 4
VIEWER_QUEUE = 500
HEARTBEAT = 15.0


class CellsServer:
    def __init__(self, address: str, port: int = 0,
                 allow: Optional[Iterable[str]] = None) -> None:
        self.token = secrets.token_hex(16)  # 128 bits, never printed
        self.allow = set(allow) if allow is not None else None
        self._lock = threading.Lock()
        self.cols = self.rows = 0
        self._grid: list = []
        self._cursor: Optional[tuple] = None
        self._status = "ok"
        self._viewers: List[queue.Queue] = []
        self._last_viewer = time.monotonic()
        self.ended = threading.Event()  # the producer closed its pipe
        self._httpd = _ThreadingTCPServer((address, port), self._make_handler())
        self.address, self.port = self._httpd.server_address[:2]
        self.url = f"http://{self.address}:{self.port}/{self.token}/"

    # -- producer side -------------------------------------------------------

    def feed(self, event: dict) -> None:
        """Apply one protocol event to the grid and pass it to every viewer."""
        kind = event.get("t")
        with self._lock:
            if kind == "hello":
                cols, rows = int(event["cols"]), int(event["rows"])
                if not (0 < cols <= 1000 and 0 < rows <= 1000):
                    return
                self.cols, self.rows = cols, rows
                self._grid = [None] * (cols * rows)
                self._cursor = None
            elif kind in ("full", "diff"):
                if not self.cols:
                    return
                self._apply(event.get("spans") or [])
            elif kind == "cursor":
                x, y = event.get("x"), event.get("y")
                self._cursor = None if x is None or y is None else (int(x), int(y))
            elif kind == "status":
                self._status = str(event.get("state", "ok"))
            else:
                return  # unknown: ignored, so the protocol can grow
            self._broadcast(json.dumps(event, separators=(",", ":")))

    def _apply(self, spans: list) -> None:
        for span in spans:
            try:
                x, y, fg, bg, at, text = span
            except (TypeError, ValueError):
                continue
            if not (0 <= y < self.rows):
                continue
            for i, ch in enumerate(text):
                if 0 <= x + i < self.cols:
                    self._grid[y * self.cols + x + i] = (fg, bg, at, ch)

    def _broadcast(self, line: str) -> None:
        for q in list(self._viewers):
            try:
                q.put_nowait(line)
            except queue.Full:  # too slow: drop it, the page reconnects and is re-primed
                self._viewers.remove(q)
                q.put(None)

    def _snapshot(self) -> List[str]:
        """The events that bring a new viewer to the current screen."""
        if not self.cols:
            return []
        out = [json.dumps({"t": "hello", "v": 1, "cols": self.cols, "rows": self.rows},
                          separators=(",", ":"))]
        spans = []
        for y in range(self.rows):
            run = None
            for x in range(self.cols):
                c = self._grid[y * self.cols + x]
                if c is None:
                    run = None
                    continue
                fg, bg, at, ch = c
                if run is not None and run[2:5] == [fg, bg, at]:
                    run[5] += ch
                else:
                    run = [x, y, fg, bg, at, ch]
                    spans.append(run)
        out.append(json.dumps({"t": "full", "spans": spans}, separators=(",", ":")))
        cur = self._cursor
        out.append(json.dumps({"t": "cursor", "x": cur and cur[0], "y": cur and cur[1]},
                              separators=(",", ":")))
        if self._status != "ok":
            out.append(json.dumps({"t": "status", "state": self._status}))
        return out

    # -- viewers ---------------------------------------------------------------

    def viewers(self) -> int:
        with self._lock:
            return len(self._viewers)

    def idle_seconds(self) -> float:
        """Seconds since the last viewer left, 0 while someone watches."""
        with self._lock:
            return 0.0 if self._viewers else time.monotonic() - self._last_viewer

    def _join(self) -> Optional[queue.Queue]:
        with self._lock:
            if len(self._viewers) >= MAX_VIEWERS:
                return None
            q: queue.Queue = queue.Queue(VIEWER_QUEUE)
            for line in self._snapshot():
                q.put_nowait(line)
            self._viewers.append(q)
            return q

    def _leave(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._viewers:
                self._viewers.remove(q)
            self._last_viewer = time.monotonic()

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args) -> None:  # the path holds the token
                pass

            def deny(self, code: int) -> None:
                try:
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except OSError:
                    pass

            def do_GET(self) -> None:  # noqa: N802
                if server.allow is not None and self.client_address[0] not in server.allow:
                    return self.deny(403)
                path = self.path.split("?", 1)[0]
                base = f"/{server.token}/"
                if path == base:
                    return self.page()
                if path == base + "events":
                    return self.events()
                return self.deny(404)

            def page(self) -> None:
                body = PAGE.encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def events(self) -> None:
                q = server._join()
                if q is None:
                    return self.deny(503)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while True:
                        try:
                            line = q.get(timeout=HEARTBEAT)
                        except queue.Empty:
                            self.wfile.write(b":\n\n")
                            self.wfile.flush()
                            continue
                        if line is None:
                            break
                        self.wfile.write(b"data: " + line.encode() + b"\n\n")
                        self.wfile.flush()
                except OSError:
                    pass  # the viewer went away: normal
                finally:
                    server._leave(q)

        return Handler

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        """Tell viewers the cast stopped, end their streams, close the listener."""
        with self._lock:
            for q in self._viewers:
                try:
                    q.put_nowait(json.dumps({"t": "status", "state": "stopped"}))
                    q.put_nowait(None)
                except queue.Full:
                    pass
        self._httpd.shutdown()
        self._httpd.server_close()
