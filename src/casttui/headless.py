"""Headless commands: cast a page, stop, report status, without the UI.

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


def exit_now(code: int) -> None:
    """Exit without waiting for library threads (zeroconf, pychromecast).

    Their non-daemon threads can delay a normal exit by seconds, which a
    capped caller would count against its own timeout.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
