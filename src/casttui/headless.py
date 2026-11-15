"""Headless commands: cast a page or the desktop, stop, report status, without
the UI.

Used by scripts and by tetron-wm (``cast_command = "casttui"``), which runs
these through a capped subprocess, so every command:

- finishes within a hard timeout (a dead network must never hang the caller),
- prints one line, on stdout for success and stderr for failure,
- exits 0 on success and 1 on failure,
- never prints the URL path or query: tetron-wm puts a secret token there, and
  the screen it leads to can show secrets. Only scheme, host and port appear.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

# Hard limit for one headless command, in seconds. Connect plus a DashCast
# force launch takes about 5 s on a basic Chromecast; 20 s leaves room for a
# slow mDNS scan without letting a broken network stall tetron-wm.
TIMEOUT = 20.0


def safe_url(url: str) -> str:
    """The URL with path, query and fragment removed, safe to print.

    "http://10.0.0.5:8765/9f3c.../" becomes "http://10.0.0.5:8765".
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def resolve_device(service, device_arg: Optional[str], last_device: Optional[str]):
    """Pick the device the same way the UI does, then return it connected.

    Order: ``-d`` if given, else the remembered ``last_device`` (a name or an
    address), else the only device a scan finds. Several devices and no hint is
    an error rather than a guess, because casting a screen to the wrong TV is
    worse than failing.
    """
    target = device_arg or last_device
    if target:
        return service.connect(target)
    found = service.discover()
    if len(found) == 1:
        return found[0]
    if not found:
        raise RuntimeError("no Chromecast found; pass -d NAME_OR_IP")
    names = ", ".join(d.name for d in found)
    raise RuntimeError(f"several Chromecasts found ({names}); pass -d NAME_OR_IP")


def status_line(device) -> str:
    """One line for ``--status``: device name, running app, and its state.

    The app is the receiver's display name in lower case ("dashcast",
    "default media receiver", ...) or "idle" when no app runs (the backdrop).
    """
    cast = device._cast
    app = (cast.app_display_name or "").lower()
    if not app or app == "backdrop" or cast.is_idle:
        return f"{device.name}: idle"
    state = cast.media_controller.status.player_state or ""
    return f"{device.name}: {app}" + (f" {state.lower()}" if state and state != "UNKNOWN" else "")


def run_with_timeout(work: Callable[[], str], timeout: float) -> str:
    """Run ``work`` on a daemon thread and return its result line.

    The thread is a daemon so a call stuck in pychromecast does not keep the
    process alive: the caller exits and the thread dies with it. Raises
    TimeoutError when the limit passes, or re-raises what ``work`` raised.
    """
    box: dict = {}

    def target() -> None:
        try:
            box["out"] = work()
        except BaseException as e:  # noqa: BLE001  carried to the caller
            box["err"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"no answer within {timeout:.0f} s")
    if "err" in box:
        raise box["err"]
    return box["out"]


def run(action: str, url: Optional[str] = None, device: Optional[str] = None,
        config=None, service=None, timeout: float = TIMEOUT) -> int:
    """Do one headless ``action`` ("page", "stop" or "status"); return the exit code.

    ``service`` and ``config`` are injectable for tests; by default a real
    :class:`CastService` and the saved config are used. The config is only
    read, never saved: a headless command does not change what the UI
    reconnects to.
    """
    if service is None:
        from .service import CastService
        service = CastService()
    if config is None:
        from .config import Config
        config = Config.load()

    def work() -> str:
        dev = resolve_device(service, device, config.last_device)
        service.device = dev  # no status listener: nothing here watches pushes
        if action == "page":
            service.cast_site(url)
            return f"casting {safe_url(url)} on {dev.name}"
        if action == "stop":
            service.stop()  # catt's stop quits the app, DashCast included
            return f"stopped {dev.name}"
        return status_line(dev)

    try:
        print(run_with_timeout(work, timeout))
        return 0
    except Exception as e:  # noqa: BLE001  one clean line, never a traceback
        msg = str(e) or type(e).__name__
        if url:
            msg = msg.replace(url, safe_url(url))  # a library may echo the URL
        print(f"casttui: {msg}", file=sys.stderr)
        return 1
    finally:
        try:
            service.close()
        except Exception:  # noqa: BLE001
            pass


# Leave the desktop cast when nobody has watched the stream for this long
# (the TV was switched to another app, or lost the network for good).
DESKTOP_IDLE = 120.0

# After casting, how long the device may take to report the cast app as
# running before "another app" means the cast was stopped.
DESKTOP_GRACE = 15.0


def desktop(device: Optional[str] = None, config=None, service=None,
            stop: Optional[threading.Event] = None, out=print) -> int:
    """Cast the live desktop until Ctrl+C, ``casttui --stop``, or an error.

    Runs the capture and stream server here, casts the player page through
    DashCast, then watches: the capture must keep running, the device must
    keep running the app that was cast (``casttui --stop`` or the TV remote
    ends it), and somebody must be watching the stream. Ctrl+C quits the app
    on the TV too, so it does not keep showing a frozen picture.
    """
    if service is None:
        from .service import CastService
        service = CastService()
    if config is None:
        from .config import Config
        config = Config.load()
    stop = stop or threading.Event()
    server = None

    def connect():
        dev = resolve_device(service, device, config.last_device)
        service.device = dev
        return dev

    try:
        dev = run_with_timeout(connect, TIMEOUT)
        # Capture start (10 s at most) plus the cast itself.
        server = run_with_timeout(lambda: service.cast_desktop(
            config.desktop_backend, config.desktop_fps, config.desktop_scale,
            config.desktop_audio, config.desktop_receiver), TIMEOUT + 10)
        backend = server.backend
        cast_app = getattr(getattr(dev, "_cast", None), "app_id", None)
        out(f"CASTING DESKTOP ({backend}) to {dev.name} from {safe_url(server.url)}. "
            "Ctrl+C or casttui --stop ends it.")
        started = time.monotonic()
        while not stop.wait(1.0):
            if server.ended.is_set():
                raise RuntimeError(f"screen capture stopped: {server.error()}")
            running = getattr(getattr(dev, "_cast", None), "app_id", None)
            if cast_app and running != cast_app and time.monotonic() - started > DESKTOP_GRACE:
                out(f"stopped: {dev.name} is no longer showing the desktop")
                return 0
            if server.idle_seconds() > DESKTOP_IDLE:
                out(f"stopped: nobody watched for {DESKTOP_IDLE:.0f} s")
                return 0
        return 0
    except KeyboardInterrupt:
        try:
            run_with_timeout(lambda: service.stop() or "", 10)
        except Exception:  # noqa: BLE001  the TV is best effort on the way out
            pass
        out("stopped")
        return 0
    except Exception as e:  # noqa: BLE001  one clean line, never a traceback
        msg = str(e) or type(e).__name__
        if server is not None:
            msg = msg.replace(server.url, safe_url(server.url))
        print(f"casttui: {msg}", file=sys.stderr)
        return 1
    finally:
        service.close()  # stops the capture and the server


def cells(device: Optional[str] = None, config=None, service=None, stdin=None,
          stop: Optional[threading.Event] = None, out=None, no_cast: bool = False) -> int:
    """Serve a tetron-wm cell stream read from ``stdin`` and cast it.

    ``stdin`` carries one JSON event per line (see :mod:`casttui.cells`). The
    cast ends when the pipe closes (tetron-wm quit or ``cast off``), on Ctrl+C,
    when the device leaves the app (``casttui --stop``, the TV remote), or
    after DESKTOP_IDLE seconds without a viewer; the TV app is quit too, so it
    does not keep showing a frozen screen. With ``no_cast`` there is no device:
    the URL (a secret) is printed for a browser on this LAN and the stream is
    served until the pipe closes.
    """
    import json

    out = out or (lambda m: print(m, flush=True))  # a pipe reader needs each line at once
    stdin = stdin if stdin is not None else sys.stdin
    stop = stop or threading.Event()
    if not no_cast:
        if service is None:
            from .service import CastService
            service = CastService()
        if config is None:
            from .config import Config
            config = Config.load()
    server = None

    def reader() -> None:
        for line in stdin:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and server is not None:
                try:
                    server.feed(event)
                except (KeyError, TypeError, ValueError):
                    continue
        if server is not None:
            server.ended.set()

    try:
        if no_cast:
            from .cells import CellsServer
            from catt.util import get_local_ip
            server = CellsServer(get_local_ip("192.0.2.1") or "127.0.0.1")
            server.start()
            print("warning: anyone on this network with this URL sees the screen", file=sys.stderr)
            out(server.url)
        else:
            def connect():
                dev = resolve_device(service, device, config.last_device)
                service.device = dev
                return dev
            dev = run_with_timeout(connect, TIMEOUT)
            server = service.cells_server()
        threading.Thread(target=reader, daemon=True).start()
        if not no_cast:
            run_with_timeout(lambda: service.show_page(server) or "", TIMEOUT)
            cast_app = getattr(getattr(dev, "_cast", None), "app_id", None)
            out(f"casting cells on {dev.name}")
        started = time.monotonic()
        while not stop.wait(0.5):
            if server.ended.is_set():
                break
            if no_cast:
                continue
            running = getattr(getattr(dev, "_cast", None), "app_id", None)
            if cast_app and running != cast_app and time.monotonic() - started > DESKTOP_GRACE:
                out(f"stopped: {dev.name} is no longer showing the screen")
                return 0
            if server.idle_seconds() > DESKTOP_IDLE:
                out(f"stopped: nobody watched for {DESKTOP_IDLE:.0f} s")
                return 0
        if not no_cast:
            try:
                run_with_timeout(lambda: service.stop() or "", 10)
            except Exception:  # noqa: BLE001  the TV is best effort on the way out
                pass
        out("stopped")
        return 0
    except KeyboardInterrupt:
        if not no_cast:
            try:
                run_with_timeout(lambda: service.stop() or "", 10)
            except Exception:  # noqa: BLE001
                pass
        out("stopped")
        return 0
    except Exception as e:  # noqa: BLE001  one clean line, never a traceback
        msg = str(e) or type(e).__name__
        if server is not None:
            msg = msg.replace(server.url, safe_url(server.url))
        print(f"casttui: {msg}", file=sys.stderr)
        return 1
    finally:
        if no_cast:
            if server is not None:
                server.stop()
        else:
            service.close()  # stops the server


def exit_now(code: int) -> None:
    """Exit without waiting for library threads (zeroconf, pychromecast).

    Their non-daemon threads can delay a normal exit by seconds, which a
    capped caller would count against its own timeout.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
