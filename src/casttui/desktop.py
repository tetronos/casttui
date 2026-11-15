"""Cast the live desktop: capture, encode, serve, play through DashCast.

Pipeline (plan Part B)::

    ffmpeg (x11grab | wf-recorder | fbdev | test pattern)
      -> H.264 fragmented MP4 on stdout, one fragment per 100 ms
    Fmp4Splitter: cuts the byte stream into the init segment (ftyp+moov) and
      fragments (moof+mdat), and marks fragments that start with a keyframe
    DesktopServer: a small HTTP server on the LAN address that reaches the
      Chromecast, under a secret random path
        GET /<token>/        the player page (PLAYER_HTML, no external requests)
        GET /<token>/stream  init segment + current GOP, then live fragments
    The page is loaded through DashCast; DashCast navigates top level to it, so
    a plain http:// LAN page works (no HTTPS mixed content block). The page
    feeds the stream to Media Source Extensions with a small buffer and keeps
    itself within about half a second of the live edge.

Security (the screen can show secrets): casting is opt in per run, the path
carries a 128 bit random token that is never printed or logged, the server
binds only the LAN address (not 0.0.0.0), answers GET only, only to the
device address and this host, and to at most two stream viewers.
"""
from __future__ import annotations

import collections
import os
import queue
import secrets
import shutil
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Callable, Iterable, List, Optional

from .fileserver import _ThreadingTCPServer

BACKENDS = ("auto", "x11", "wayland", "fbdev", "test")

# At most this many simultaneous stream viewers (the TV, plus one browser tab
# for checking). More is refused: nobody else should be watching.
MAX_VIEWERS = 2

# Fragments queued for one viewer before it is dropped as too slow. At 100 ms
# per fragment this is about 10 s, far past what the player tolerates anyway.
VIEWER_QUEUE = 100


# -- capture backend detection and the ffmpeg command ------------------------

def detect_backend(env=os.environ, which: Callable = shutil.which,
                   exists: Callable = os.path.exists) -> str:
    """Pick how to capture the screen from the session environment.

    Order: wlroots Wayland (wf-recorder present), X11 ($DISPLAY, which also
    covers XWayland-less X sessions), then the bare console framebuffer.
    GNOME and KDE on Wayland need the xdg-desktop-portal, not done yet.
    """
    if env.get("WAYLAND_DISPLAY"):
        if which("wf-recorder"):
            return "wayland"
        raise RuntimeError("Wayland desktop capture needs wf-recorder "
                           "(GNOME/KDE portal capture is not supported yet)")
    if env.get("DISPLAY"):
        return "x11"
    if exists("/dev/fb0"):
        return "fbdev"
    raise RuntimeError("no screen to capture (no $DISPLAY, $WAYLAND_DISPLAY or /dev/fb0)")


def encode_args(fps: int, scale: int, audio: bool) -> List[str]:
    """The shared ffmpeg tail: low latency H.264 (and AAC) in fragmented MP4.

    - zerolatency + no B frames: each frame is sent as soon as it is encoded.
    - a keyframe every second (-g fps): a late viewer starts within a second.
    - 100 ms fragments: the player gets data ten times a second, which is
      what keeps the end to end delay well under a second.
    - High profile level 4.1: decodable by every Chromecast at up to 1080p30.
    """
    vf = [f"scale=-2:{scale}"] if scale else []
    vf.append("format=yuv420p")
    args = [
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-profile:v", "high", "-level", "4.1",
        "-g", str(fps), "-bf", "0",
    ]
    if audio:
        args += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    return args + [
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration", "100000",
        "pipe:1",
    ]


def capture_cmd(backend: str, fps: int = 30, scale: int = 720, audio: bool = False,
                env=os.environ) -> List[str]:
    """The argv that writes the live desktop as fragmented MP4 to stdout.

    Wayland goes through ``sh -c`` because wf-recorder captures and ffmpeg
    re-encodes; every other backend is one ffmpeg process.
    """
    head = ["ffmpeg", "-nostdin", "-loglevel", "error", "-fflags", "nobuffer"]
    # Audio is what the speakers play: the default sink's monitor source.
    sound = ["-f", "pulse", "-i", "@DEFAULT_MONITOR@"] if audio else []
    tail = encode_args(fps, scale, audio)
    if backend == "x11":
        # No -video_size: x11grab then grabs the whole screen.
        src = ["-f", "x11grab", "-framerate", str(fps), "-i", env.get("DISPLAY", ":0")]
    elif backend == "fbdev":
        src = ["-f", "fbdev", "-framerate", str(fps), "-i", "/dev/fb0"]
    elif backend == "test":
        # A moving test pattern with a frame counter: checks the whole chain
        # (and the delay, roughly) without showing anything private.
        src = ["-re", "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={fps}"]
    elif backend == "wayland":
        # EXPERIMENTAL, untested: wf-recorder writes raw frames in NUT, which
        # ffmpeg reads from the pipe without probing delay.
        ff = " ".join(head + ["-f", "nut", "-i", "-"] + sound + tail)
        return ["sh", "-c", f"wf-recorder -r {fps} -c rawvideo -m nut -f /dev/stdout 2>/dev/null | {ff}"]
    else:
        raise ValueError(f"unknown capture backend {backend!r}")
    return head + src + sound + tail


# -- fragmented MP4 parsing ---------------------------------------------------

def boxes(data: bytes, start: int = 0, end: Optional[int] = None) -> Iterable[tuple]:
    """Yield (type, payload_start, box_end) for each complete box in data[start:end]."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        head = 8
        if size == 1:  # 64 bit size follows the type
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            head = 16
        if size < head or pos + size > end:
            return
        yield kind, pos + head, pos + size
        pos += size


def moof_keyframe(moof: bytes) -> bool:
    """True when the first sample of the first track in this moof is a sync
    sample (a keyframe), so a decoder can start at this fragment.

    The first sample's flags come from, in order: trun first_sample_flags,
    the trun per sample flags, the tfhd default_sample_flags. A sample is a
    keyframe when the sample_is_non_sync_sample bit (0x10000) is clear. With
    none of these present the fragment is assumed to be a keyframe, which is
    what ffmpeg's frag_keyframe produces.
    """
    for kind, p, e in boxes(moof, 8):
        if kind != b"traf":
            continue
        default = None
        for k, q, f in boxes(moof, p, e):
            flags = struct.unpack_from(">I", moof, q)[0] & 0xFFFFFF
            if k == b"tfhd":
                off = q + 8  # version/flags, track_ID
                off += 8 if flags & 0x1 else 0
                off += 4 if flags & 0x2 else 0
                off += 4 if flags & 0x8 else 0
                off += 4 if flags & 0x10 else 0
                if flags & 0x20:
                    default = struct.unpack_from(">I", moof, off)[0]
            elif k == b"trun":
                off = q + 8  # version/flags, sample_count
                off += 4 if flags & 0x1 else 0
                if flags & 0x4:
                    first = struct.unpack_from(">I", moof, off)[0]
                else:
                    off += 4 if flags & 0x100 else 0
                    off += 4 if flags & 0x200 else 0
                    first = struct.unpack_from(">I", moof, off)[0] if flags & 0x400 else default
                return first is None or not first & 0x10000
        return default is None or not default & 0x10000
    return True


def codec_string(init: bytes, audio: bool = False) -> str:
    """The MSE codecs string for this stream, read from the avcC box.

    avcC starts with version, profile, profile compatibility and level, which
    is exactly the "avc1.PPCCLL" string; reading it keeps the page right if
    the encoder settings change. Falls back to High 4.1.
    """
    i = init.find(b"avcC")
    video = "avc1.%02x%02x%02x" % tuple(init[i + 5:i + 8]) if i >= 0 else "avc1.640029"
    return video + (",mp4a.40.2" if audio else "")


class Fmp4Splitter:
    """Cut a fragmented MP4 byte stream into the init segment and fragments.

    :meth:`feed` takes bytes as they arrive from ffmpeg (any split) and returns
    a list of ("init", bytes) and ("frag", bytes, is_keyframe) items. ftyp and
    moov form the init; each moof plus its mdat forms a fragment.
    """

    def __init__(self) -> None:
        self.buf = bytearray()
        self.init = bytearray()
        self.moof: Optional[bytes] = None

    def feed(self, data: bytes) -> list:
        self.buf += data
        out = []
        consumed = 0
        for kind, p, e in boxes(self.buf):
            box = bytes(self.buf[consumed:e])
            consumed = e
            if kind == b"ftyp":
                self.init = bytearray(box)
            elif kind == b"moov":
                self.init += box
                out.append(("init", bytes(self.init)))
            elif kind == b"moof":
                self.moof = box
            elif kind == b"mdat" and self.moof is not None:
                out.append(("frag", self.moof + box, moof_keyframe(self.moof)))
                self.moof = None
            # styp, sidx, free, mfra: not needed by the player, dropped.
        del self.buf[:consumed]
        return out


# -- the player page ----------------------------------------------------------

# Embedded so the page needs nothing from the internet and nothing but this
# server. CODECS is filled in per request from the live stream's init segment;
# MUTED is "muted" when there is no audio (lets autoplay start everywhere).
PLAYER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>casttui desktop</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{margin:0;height:100%;background:#000;overflow:hidden}
video{width:100%;height:100%;object-fit:contain;background:#000}
#msg{position:fixed;left:2em;bottom:2em;color:#ccc;font:24px sans-serif;
     background:rgba(0,0,0,.6);padding:.3em .6em;display:none}
</style></head><body>
<video id="v" autoplay playsinline MUTED></video><div id="msg"></div>
<script>
"use strict";
const MIME = 'video/mp4; codecs="CODECS"';
const v = document.getElementById("v"), msg = document.getElementById("msg");
function say(t) { msg.textContent = t; msg.style.display = t ? "block" : "none"; }
function retry(t) { say(t); setTimeout(() => location.reload(), 2000); }

if (!window.MediaSource || !MediaSource.isTypeSupported(MIME)) {
  say("this device can not play " + MIME);
} else {
  const ms = new MediaSource();
  v.src = URL.createObjectURL(ms);
  ms.addEventListener("sourceopen", () => run(ms), { once: true });
}

async function run(ms) {
  const sb = ms.addSourceBuffer(MIME);
  let pending = [];
  // Append whatever arrived while the buffer was busy as one block.
  function pump() {
    if (sb.updating || !pending.length) return;
    let n = 0; for (const c of pending) n += c.length;
    const all = new Uint8Array(n); let o = 0;
    for (const c of pending) { all.set(c, o); o += c.length; }
    pending = [];
    try { sb.appendBuffer(all); } catch (e) { retry("buffer error, reconnecting"); }
  }
  sb.addEventListener("updateend", pump);

  // Every half second: stay near the live edge and keep the buffer small.
  setInterval(() => {
    const b = v.buffered;
    if (!b.length) return;
    const start = b.start(0), end = b.end(b.length - 1), lag = end - v.currentTime;
    if (v.currentTime < start || lag > 0.6) v.currentTime = Math.max(start, end - 0.2);
    v.playbackRate = lag > 0.3 ? 1.05 : 1.0;
    if (!sb.updating && v.currentTime - start > 3) sb.remove(start, v.currentTime - 2);
    if (v.paused) v.play().catch(() => {});
  }, 500);

  let res;
  try { res = await fetch("stream", { cache: "no-store" }); }
  catch (e) { return retry("can not reach casttui, retrying"); }
  if (!res.ok) return retry("casttui said " + res.status + ", retrying");
  const reader = res.body.getReader();
  say("");
  for (;;) {
    let r;
    try { r = await reader.read(); } catch (e) { break; }
    if (r.done) break;
    pending.push(r.value); pump();
  }
  retry("stream ended");
}
</script></body></html>
"""


# -- the server ---------------------------------------------------------------

class DesktopServer:
    """Run the capture command and serve its stream under a secret path.

    One ffmpeg process feeds every viewer: a reader thread splits its output
    and fans fragments out to per viewer queues. The init segment and the
    fragments since the last keyframe are kept, so a viewer that connects
    late (or reconnects) starts playing at once instead of waiting for the
    next keyframe.
    """

    def __init__(self, cmd: List[str], address: str, port: int = 0,
                 allow: Optional[Iterable[str]] = None, audio: bool = False) -> None:
        self.cmd = cmd
        self.audio = audio
        self.token = secrets.token_hex(16)  # 128 bits, never printed
        self.allow = set(allow) if allow is not None else None
        self._lock = threading.Lock()
        self._init: Optional[bytes] = None
        self._init_ready = threading.Event()
        self._gop: List[bytes] = []
        self._viewers: List[queue.Queue] = []
        self._last_viewer = time.monotonic()
        self._errors: collections.deque = collections.deque(maxlen=10)
        self.ended = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._httpd = _ThreadingTCPServer((address, port), self._make_handler())
        self.address, self.port = self._httpd.server_address[:2]
        self.url = f"http://{self.address}:{self.port}/{self.token}/"
        self._threads: List[threading.Thread] = []

    # -- capture side ------------------------------------------------------

    def _read_stdout(self) -> None:
        """Split ffmpeg output and hand each fragment to every viewer."""
        splitter = Fmp4Splitter()
        assert self._proc and self._proc.stdout
        while True:
            data = self._proc.stdout.read1(65536)
            if not data:
                break
            for item in splitter.feed(data):
                if item[0] == "init":
                    self._init = item[1]
                    self._init_ready.set()
                    continue
                _, frag, key = item
                with self._lock:
                    if key:
                        self._gop = [frag]
                    elif self._gop:
                        self._gop.append(frag)
                    for q in list(self._viewers):
                        try:
                            q.put_nowait(frag)
                        except queue.Full:  # too slow: drop it, it reconnects
                            self._viewers.remove(q)
                            q.queue.clear()
                            q.put_nowait(None)
        self.ended.set()
        with self._lock:
            for q in self._viewers:
                q.put(None)

    def _read_stderr(self) -> None:
        """Keep ffmpeg's last error lines for the failure message."""
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            self._errors.append(line.decode(errors="replace").strip())

    def error(self) -> str:
        """ffmpeg's last error line, or a generic message."""
        lines = [x for x in self._errors if x]
        return lines[-1] if lines else "the screen capture stopped"

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Wait until the encoder produced its init segment (or died)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._init_ready.wait(0.1):
                return True
            if self.ended.is_set():
                return False
        return False

    def codecs(self) -> str:
        return codec_string(self._init or b"", self.audio)

    # -- viewers -------------------------------------------------------------

    def viewers(self) -> int:
        with self._lock:
            return len(self._viewers)

    def idle_seconds(self) -> float:
        """Seconds since the last viewer left, 0 while someone watches."""
        with self._lock:
            if self._viewers:
                return 0.0
            return time.monotonic() - self._last_viewer

    def _join(self) -> Optional[queue.Queue]:
        """Register a viewer primed with the init segment and current GOP."""
        with self._lock:
            if len(self._viewers) >= MAX_VIEWERS or self._init is None:
                return None
            q: queue.Queue = queue.Queue(VIEWER_QUEUE + len(self._gop) + 1)
            q.put_nowait(self._init)
            for frag in self._gop:
                q.put_nowait(frag)
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
            # HTTP/1.0 without Content-Length: the stream is read until close.
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
                base = f"/{server.token}/"
                if self.path == base:
                    return self.page()
                if self.path == base + "stream":
                    return self.stream()
                return self.deny(404)

            def page(self) -> None:
                server.wait_ready(10)
                body = (PLAYER_HTML.replace("CODECS", server.codecs())
                        .replace("MUTED", "" if server.audio else "muted")).encode()
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

            def stream(self) -> None:
                server.wait_ready(10)
                q = server._join()
                if q is None:
                    return self.deny(503)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while True:
                        chunk = q.get()
                        if chunk is None:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except OSError:
                    pass  # the viewer went away: normal
                finally:
                    server._leave(q)

        return Handler

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self._proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      start_new_session=True)
        for fn in (self._read_stdout, self._read_stderr, self._httpd.serve_forever):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        """Stop the capture (its whole process group, for the sh pipeline)
        and the server, and end every viewer's stream."""
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, 15)
                self._proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self._proc.pid, 9)
                except OSError:
                    pass
        self._httpd.shutdown()
        self._httpd.server_close()
