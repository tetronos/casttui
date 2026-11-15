# How to cast a desktop or a web page to a Chromecast with casttui

Casting the desktop is split between two programs, and casting is an optional addon for tetron-wm. casttui casts what is on a pixel screen (an X11 desktop, the console framebuffer), any web page, and the cell stream of [tetron-wm](https://tetronos.github.io/tetron-wm/), and it controls the TV. tetron-wm only pipes its text desktop to `casttui --cells` and opens no port. This page is the casttui side. The tetron-wm side is in its own `docs/HOWTO_tetron-wm_cast_the_desktop_to_a_chromecast.md`.

## Which one to use

| You want to show | Use | Delay | Needs |
| --- | --- | --- | --- |
| The screen of an X11 desktop or the Linux console | `casttui --desktop` | about 0.5 s | ffmpeg, DashCast on the TV |
| A web page you or a program serves | `casttui --page URL` | none | the page reachable on the LAN |
| The tetron-wm desktop | `tetron-wm cast` (it pipes the screen to `casttui --cells`) | about 0.1 s | casttui |
| A file or link | `casttui FILE` or the Source pane | n/a | n/a |

The Chromecast and this computer must be on the same LAN segment, because discovery is mDNS.

## Pick the device once

`casttui --status` prints the device and what it shows (`Living Room TV: idle`). The device is chosen with `-d NAME_OR_IP`, else the remembered `last_device` (set when you connect in the interface), else the only device found. With two devices and nothing remembered, pass `-d`.

## Cast the desktop

```sh
casttui --desktop        # captures this screen until Ctrl+C or casttui --stop
casttui --stop           # from another terminal: ends the cast and quits the TV app
```

In the interface press `D`, type `:desktop`, or use the Source button. A red **CASTING DESKTOP [s: stop]** stays on the bottom line while it runs, and `s` stops it.

Capture backends (`desktop_backend`): `auto` picks X11 when `$DISPLAY` is set, else the console framebuffer. `x11` uses x11grab, `fbdev` reads `/dev/fb0` (your user needs the `video` group), `wayland` uses `wf-recorder` on wlroots compositors and is experimental, and `test` casts a moving test pattern for checking the TV chain without a screen. GNOME and KDE on Wayland are not supported. Related settings: `desktop_fps` (30), `desktop_scale` (height, 720; 0 keeps the screen size), `desktop_audio` (off), and `desktop_receiver` (`dashcast`, or `media` for the Default Media Receiver with several seconds of delay, for devices without DashCast).

## Cast a web page

```sh
casttui --page http://192.168.1.43:8099/
casttui --status         # Living Room TV: dashcast
casttui --stop
```

This is the same call tetron-wm makes. The page is loaded as a top level page by [DashCast](https://github.com/swiergot/dashcast), so plain `http://` on the LAN works. Print one line and exit 0 on success, exit 1 on failure, with a hard 20 second limit so a dead network never hangs a script. The URL path and query are never printed, because callers put a secret token there. Only `http` and `https` URLs are accepted.

Chromecast web pages render at about 1280x720 on older models, so size canvases from `innerWidth` and `innerHeight` and test on the device.

## Cast a tetron-wm desktop (--cells)

```sh
casttui --cells [-d DEVICE]          # reads the cell stream on stdin, casts it, quits the TV app when stdin closes
casttui --cells --no-cast            # no device: prints a URL (a secret) for a browser on this LAN
```

You normally never run this yourself: `tetron-wm cast` starts it and feeds it. casttui keeps the current grid, so a viewer that connects late is primed with the whole screen, then gets live changes. It prints one line when the cast is up (`casting cells on DEVICE`) and exits when stdin closes, on Ctrl+C, when the TV leaves the app, or after 2 minutes with nobody watching. At most 4 viewers. The stream protocol (one JSON object per line: `hello`, `full`, `diff`, `cursor`, `status`) is described in the tetron-wm howto next to this one.

## What the TV can see, and who can connect

Everything on the screen is visible on the TV, so treat it like sharing a screen. The desktop stream is served only on the LAN address that reaches the Chromecast, under a random 128 bit path that is never shown, to the Chromecast and this computer only, and to at most 2 viewers. The cast ends on Ctrl+C or `--stop`, when the TV switches to another app, when capture dies (the ffmpeg error is shown), or after 2 minutes with nobody watching. There is no TLS: anyone who can sniff the LAN can read the stream, so do not cast on a network you do not trust.

## Troubleshooting

`no device found`: the TV is asleep or on another network segment. Run `casttui --status -d IP`. The cast starts but the TV stays black: wait up to 10 seconds, then check `casttui --status` shows `dashcast`. `ffmpeg` missing: install it (`sudo apt install ffmpeg`). `/dev/fb0` permission denied: add your user to the `video` group and log in again. The TV stays blank while the stream is running: run with `CASTTUI_DEBUG=1 casttui --desktop`, which adds a status line and a short event log (survives page reloads) at the top of the TV picture, and read what the video element reports. A desktop cast ends by itself after 2 minutes: nothing connected, usually a firewall between the computer and the TV.

## See also

casttui `README.md` (all keys and settings), `CHANGELOG.md`, and the tetron-wm howto next to this file in the tetron-wm repository.
