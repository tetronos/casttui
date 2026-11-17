# Changelog

## [Unreleased]

### Added
- A permanent red **[s: STOP]** button on the bottom line of every pane. `s` or a click stops whatever the chosen device is showing, including a cast started by another app. While the desktop is cast it reads CASTING DESKTOP [s: stop] as before.

## [0.15.0] - 2026-11-15

### Added
- Headless commands for scripts and tetron-wm: `--page URL` (show a web page through DashCast), `--stop`, `--status`. One line of output, exit 0 or 1, a 20 second limit. The device is `-d`, the remembered one, or the only one found. A page URL's path and query are never printed.
- Cast the desktop: `D`, `:desktop` or `casttui --desktop`. ffmpeg captures the screen (X11, framebuffer, experimental wlroots Wayland, or a test pattern) as low delay H.264, casttui serves it with a small player page loaded through DashCast, about half a second behind the screen. Served only on the LAN address, under a secret random path, only to the Chromecast and this computer. A red CASTING DESKTOP [s: stop] item stays on the key bar while it runs.
- `casttui --cells [-d DEVICE]` serves and casts a tetron-wm cell stream read from stdin as JSON lines (protocol v1: hello, full, diff, cursor, status), so tetron-wm needs no server of its own. The current grid is kept, so a viewer that connects late sees the whole screen. `--cells --no-cast` prints a URL (a secret) for a browser on the LAN instead. Ends when stdin closes, on Ctrl+C, when the TV leaves the app, or after 2 minutes with nobody watching, and quits the TV app. At most 4 viewers, LAN address only, random 128 bit path.
- New settings: `desktop_fps`, `desktop_scale`, `desktop_audio`, `desktop_backend`, `desktop_receiver` (`media` plays the desktop on the Default Media Receiver, with seconds of delay).

## [0.2.0] - 2026-11-07

### Removed
- The classic Textual interface (`casttui --classic`, `casttui-classic`, `CASTTUI_UI`, the `ui` setting) and the Textual dependency. The paned interface is the only one; an old `ui` line in config.toml is ignored and kept.

### Added
- MIT license.
- A Debian package (`casttui_<version>_all.deb`) built by the release workflow: casttui and its Python libraries in `/usr/lib/casttui` (pure Python), run by the system python3 (3.11 or newer) through `/usr/bin/casttui`. It is published in the tetronos apt repository (https://tetronos.github.io/apt).

### Fixed
- The focus colour test passes with urwid 5, which layers display attributes.

## [0.1.0] - 2026-10-23

### Added
- Paned, keyboard-first interface (urwid), now the default. One screen with Devices, Source, Now playing, Config and History panes. The layout follows the terminal size: three columns from 150 columns wide, two from 80, one pane at a time with a tab strip below that.
- Every action has a key, listed by `?` and shown in a key bar for the focused pane. Keys can be rebound in a `[keys]` table. The key bar items, a Now playing control row (`[<<] [pause] [>>] [stop] [>|] vol[-][+] [mute]`) and the Source pane's `[u: URL or YouTube]` and `[w: web page]` buttons are all clickable.
- Add a Chromecast by address (`a` in Devices), saved in `devices`. `-d NAME_OR_IP` connects without scanning, and a device added by address reconnects at start without discovery.
- In-app Config pane: every setting with its value, changed with `enter` or `+`/`-` and saved at once. Saving keeps the file's comments, order and unknown keys.
- `:` command line: `set KEY VALUE`, `cast PATH|URL`, `page URL`, `connect NAME|IP`, `scan`, `quit`.
- History kept across runs in `~/.local/state/casttui/history.jsonl` (newest 200). `enter` casts an entry again, `d` removes it, and `history = false` turns it off.
- `casttui <file|url>` casts the argument once a device is connected: the remembered device, the only one found, or the one picked.
- Plain ASCII labels and borders on a bare console (`icons = auto|ascii|unicode`) and colour depth `colors = auto|mono|16|256`, honouring `NO_COLOR` and `TETRON_WM_COLORS`.
- New settings: `ui`, `transcode_scale`, `icons`, `colors`, `start_dir`, `history`, `devices`, `keys`. `casttui --help` lists them all.
- `q` quits and says playback continues on the device; `Q` stops playback first.
- `--version`.

### Changed
- The original Textual interface is kept as `casttui --classic`, `casttui-classic`, `CASTTUI_UI=classic` or `ui = "classic"`.
- Colours readable on both dark and light terminal themes at 16 colours: focus and selection use reverse video, and no bright yellow, green or red is used.

### Fixed
- Pictures cast. The receiver is now told the real file type (catt's `play_url` always said `video/mp4`), and a shown picture is no longer waited on to start playing. `.webp` gets the right type.
- A failed cast or command now shows its error instead of nothing.
- Tests no longer write to the real config or history file.

## [0.0.1]

- First version: classic Textual interface with Devices, Cast and Now playing views; local files, direct URLs, YouTube, yt-dlp sites and web pages; ffmpeg transcoding for codecs the device can not play; remembers the last device.
