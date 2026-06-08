# YT Portable

A self-contained YouTube **video / audio** downloader for **Windows, Linux and
macOS** with a local web interface, built on top of
[yt-dlp](https://github.com/yt-dlp/yt-dlp). Paste a link, pick **video** or
**audio only**, choose the quality, and download.

**No installation required.** A one-click builder assembles a portable `bin/`
folder with `yt-dlp` and `ffmpeg`/`ffprobe` and creates a launcher. Nothing is
written to the system: on Windows there are no entries in Program Files, the
registry, or PATH (it even runs on an embedded Python — no admin rights); on
Linux/macOS it runs on your existing `python3` and everything else lives inside
the same folder, which you can drop on a USB stick and run anywhere.

> Uses only the Python standard library — no Flask, no `pip`.

<p align="center">
  <img src="https://github.com/jsunyermias/yt-portable/blob/1f8da9f66809364516cc3fcffe71cd96c46c166f/assets/screenshot.png" alt="screenshot">
</p>

---

## Features

- Clean local web UI (dark theme), opens automatically in your browser.
- **Interface available in 21 languages** (auto-detected from the browser, with
  a selector showing each language's native name and an SVG flag): Spanish,
  English, French, Portuguese, Italian, German, Russian, Chinese, Japanese,
  Korean, Hindi, Bengali, Arabic, Indonesian, Urdu, Czech, Polish, Catalan,
  Esperanto, Turkish, Vietnamese — including RTL layout for Arabic and Urdu.
  (Docs are EN/ES only.)
- Video (`.mp4`, up to best available / 1080p / 720p / 480p / 360p) or
  audio-only (MP3 / M4A / Opus / WAV / FLAC).
- Optional **full playlist download** (off by default — downloads only the
  linked video), with per-item progress (e.g. `3/12`).
- Real-time progress (percentage, speed, ETA) and a **cancel** button.
- Runs **without a console window** (uses `pythonw.exe`); a big **Close** button
  in the UI stops it.
- **Self-updating dependencies**, safely: checks at most once per day, downloads
  new versions of yt-dlp/ffmpeg to a staging area, and applies them on the next
  launch (Windows can't overwrite a binary while it's in use).
- yt-dlp updates are **verified against the official SHA-256 checksum** before
  being applied; ffmpeg falls back to a second source if the primary is down.
- Automatic free-port selection and an on-screen error page if startup fails.

---

## Quick start

### Windows

1. Put `app.py` and `Setup-Portable.bat` in an empty folder.
2. Double-click **`Setup-Portable.bat`** (once). It downloads, into that same
   folder: embedded Python (`runtime/`), `yt-dlp.exe`, and `ffmpeg` (`bin/`).
   This takes a few minutes (ffmpeg is large) and installs nothing system-wide.
3. It creates **`YT Portable.lnk`** — double-click it to launch (no console
   window). `Start-Portable.bat` is an equivalent fallback launcher.
4. The UI opens at `http://127.0.0.1:8765`. Downloads land in your user's
   **Downloads** folder (`%USERPROFILE%\Downloads`, or wherever you've
   relocated it via *Properties → Location*).

After the first build you never need the builder again. Copy the folder
anywhere and it just works (Windows 10/11; uses the built-in `curl` and
PowerShell only during the build).

### Linux / macOS

1. Put `app.py` and `Setup-Portable.sh` in an empty folder.
2. Run **`./Setup-Portable.sh`** (once; make it executable first if needed:
   `chmod +x Setup-Portable.sh`). It downloads `yt-dlp` and `ffmpeg`/`ffprobe`
   into `bin/` (using your existing `python3` — no embedded interpreter, since
   essentially every Linux distro and macOS already ships one) and creates a
   launcher: `Start-Portable.sh`, plus a double-clickable **`YT Portable.desktop`**
   on Linux or **`YT Portable.command`** on macOS.
3. Launch it from your file manager / Finder, or run `./Start-Portable.sh`.
4. The UI opens at `http://127.0.0.1:8765`. Downloads land in `~/Downloads`.

After the first build you never need the builder again — copy the folder to
another machine with the same OS/architecture and it keeps working. Requires
`curl` or `wget`, and `tar`/`unzip` (all preinstalled on virtually every
distro and on macOS).

---

## How auto-update works

1. **On launch**, any update staged previously is applied first (new binaries
   moved from `_staging/` into `bin/`, old ones deleted), then the app starts.
2. **In the background** (at most once a day), it checks for a newer yt-dlp
   (GitHub) and a newer ffmpeg — from gyan.dev on Windows, johnvansickle.com
   on Linux, or evermeet.cx on macOS. If found, it downloads them to
   `_staging/`.
3. **Next launch** the staged binaries are applied.

The yt-dlp download is always verified against the official `SHA2-256SUMS`
(per-OS asset: `yt-dlp.exe` / `yt-dlp_linux` / `yt-dlp_macos`) and rejected on
mismatch. ffmpeg is verified by SHA-256 (Windows/macOS) or MD5 (Linux, the only
checksum johnvansickle publishes) when available. The Python interpreter
(embedded `runtime/` on Windows, the system's `python3` elsewhere) is
intentionally **not** auto-updated. If there's no internet or a check fails,
the app keeps running with whatever it already has.

---

## Project structure

```
.
├── app.py                 # The whole program (server + UI), stdlib only
├── Setup-Portable.bat     # One-time builder for Windows (embedded Python + binaries)
├── Setup-Portable.sh      # One-time builder for Linux/macOS (binaries only; uses python3)
├── README.md
├── LEEME.md               # Spanish docs
├── LICENSE
└── .gitignore
```

After building (these are git-ignored, not committed):

```
# Windows
├── runtime/               # Embedded Python (incl. pythonw.exe)
├── bin/                   # yt-dlp.exe, ffmpeg.exe, ffprobe.exe, versions.json
├── yt-portable.ico        # App icon, extracted from app.py for the shortcut
├── YT Portable.lnk        # Generated launcher
└── Start-Portable.bat

# Linux / macOS
├── bin/                   # yt-dlp, ffmpeg, ffprobe, versions.json
├── yt-portable.ico        # App icon, extracted from app.py for the launcher
├── YT Portable.desktop    # Generated launcher (Linux)
├── YT Portable.command    # Generated launcher (macOS)
└── Start-Portable.sh
```

---

## Configuration

A few constants at the top of `app.py`:

- `PORT` — base port (default `8765`; the app probes the next 20 if it's busy).
- Output template, quality strings, and update sources are also there.

---

## Troubleshooting

- **Nothing happens / a browser error tab opens** — check `app.log` next to
  `app.py`. If another instance is already running, the app detects it
  automatically and reopens that same UI instead of starting a new one.
- **Antivirus flags `yt-dlp.exe`** (Windows) — a common false positive; allow it.
- **The `.lnk` couldn't be created** (Windows) — use `Start-Portable.bat` instead.
- **`Setup-Portable.sh`: "Permission denied"** (Linux/macOS) — make it
  executable first: `chmod +x Setup-Portable.sh`, then run `./Setup-Portable.sh`.
- **`Setup-Portable.sh` says python3 wasn't found** — install it via your
  package manager (`apt`, `dnf`, `pacman`, Homebrew…) and run the script again.
- **macOS says the app/launcher is from an "unidentified developer"** — the
  downloaded `yt-dlp`/`ffmpeg` binaries and the generated `.command` aren't
  signed/notarized; right-click → *Open* once to approve them (or allow them
  in *System Settings → Privacy & Security*).
- **Updates never apply** — make sure the app was fully closed (via the Close
  button) so the staged binaries aren't locked (Windows) or in use.

---

## Licenses & dependencies

| Component | License | Notes |
|-----------|---------|-------|
| This project | MIT | See [LICENSE](LICENSE) |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | The Unlicense | Public domain |
| [CPython](https://www.python.org/) | PSF License | Embedded on Windows; the system's `python3` on Linux/macOS |
| [FFmpeg](https://ffmpeg.org/) | GPL 2+ | See note below |

**FFmpeg and the GPL.** FFmpeg is open source but uses a copyleft license (GPL 2+): if you *distribute* a product that bundles FFmpeg, your own code must also be GPL-compatible. This project never bundles FFmpeg — the builder downloads a portable build directly onto the user's own machine at setup time (gyan.dev on Windows, [johnvansickle.com](https://johnvansickle.com/ffmpeg/) static builds on Linux, [evermeet.cx](https://evermeet.cx/ffmpeg/) on macOS — all well-known, widely used redistributors of the official FFmpeg sources). No redistribution takes place from this project, so the GPL's copyleft clause does not apply to you as a developer or to your users. If you fork this project and change how FFmpeg is delivered (e.g. you start shipping the binary yourself), review your GPL obligations.

## Disclaimer

This is a personal-use tool. You are responsible for complying with YouTube's
Terms of Service and the copyright laws of your jurisdiction. Download only
content you have the right to.

## License

MIT — see [LICENSE](LICENSE). (Update the copyright holder line to your name.)
