#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YT Portable — YouTube video/audio downloader (Windows, self-contained).

Requires no installation: uses the embedded Python in the `runtime\\`
folder, and the `yt-dlp.exe` and `ffmpeg.exe` in the `bin\\` folder.
Uses only Python's standard library (no Flask, no pip).
"""

import os
import re
import sys
import ssl
import json
import time
import uuid
import shutil
import hashlib
import zipfile
import threading
import subprocess
import webbrowser
import urllib.request
import urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Paths and configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
BIN_DIR = BASE_DIR / "bin"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

APP_VERSION = "0.9.4"

HOST = "127.0.0.1"
PORT = 8765

# Locate yt-dlp (prefer the local .exe; otherwise fall back to PATH)
_ytdlp_local = BIN_DIR / "yt-dlp.exe"
YTDLP = str(_ytdlp_local) if _ytdlp_local.exists() else "yt-dlp"

# Locate ffmpeg (folder to pass to yt-dlp via --ffmpeg-location)
FFMPEG_LOCATION = str(BIN_DIR) if (BIN_DIR / "ffmpeg.exe").exists() else None

# Avoid popping up console windows on Windows
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# --- Dependency self-update ---
STAGING_DIR = BASE_DIR / "_staging"          # staging area (applied on next startup)
VERSIONS_FILE = BIN_DIR / "versions.json"    # installed versions
STAGED_FILE = STAGING_DIR / "staged.json"    # staged versions
CHECK_FILE = BIN_DIR / "update_check.json"   # date of the last check

YTDLP_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
YTDLP_EXE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YTDLP_SUMS_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
FFMPEG_VER_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_SHA_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"
# Fallback source if gyan.dev doesn't respond (GitHub, BtbN builds):
FFMPEG_FALLBACK_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-win64-gpl.zip"
)

# Update state (read by the UI; uses codes, not text)
update_state = {"checking": False, "pending": False, "code": "", "notes": []}
update_lock = threading.Lock()

# Job and in-progress process store
jobs = {}
procs = {}
jobs_lock = threading.Lock()

# Download queue (not persistent: lost when the program closes)
queue_order = []
queue_cv = threading.Condition(jobs_lock)
MAX_QUEUE = 16
QUEUE_ACTIVE_STATUSES = ("queued", "starting", "downloading", "processing")


# ---------------------------------------------------------------------------
# Dependency self-update (staged: applied on next startup)
# ---------------------------------------------------------------------------
def _ssl_ctx():
    """TLS context that trusts the Windows certificate store."""
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    except Exception:
        pass
    return ctx


def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "YT-Portable"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        return r.read()


def _download(url, dest, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "YT-Portable"})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r, \
            open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, length=1024 * 256)
    if dest.exists():
        dest.unlink()
    tmp.rename(dest)


def _run_text(cmd):
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20,
            creationflags=CREATE_NO_WINDOW,
        )
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _read_json(path):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {}


def _today():
    return time.strftime("%Y-%m-%d")


def should_check_today():
    """True if updates haven't been checked yet today."""
    return _read_json(CHECK_FILE).get("last") != _today()


def mark_checked_today():
    try:
        BIN_DIR.mkdir(exist_ok=True)
        CHECK_FILE.write_text(json.dumps({"last": _today()}), "utf-8")
    except Exception:
        pass


def installed_versions():
    """Installed versions: from versions.json, or by querying the binaries."""
    v = _read_json(VERSIONS_FILE)
    if not v.get("yt-dlp") and YTDLP != "yt-dlp" and Path(YTDLP).exists():
        v["yt-dlp"] = _run_text([YTDLP, "--version"])
    if not v.get("ffmpeg") and FFMPEG_LOCATION:
        line = _run_text([str(BIN_DIR / "ffmpeg.exe"), "-version"]).splitlines()
        if line:
            m = re.search(r"ffmpeg version (\S+)", line[0])
            if m:
                v["ffmpeg"] = m.group(1)
    return v


def _latest_ytdlp():
    try:
        data = json.loads(_http_get(YTDLP_API))
        return (data.get("tag_name") or "").strip()
    except Exception:
        return None


def _latest_ffmpeg():
    try:
        return _http_get(FFMPEG_VER_URL, timeout=20).decode("utf-8").strip()
    except Exception:
        return None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _parse_sha_text(text):
    """Returns the first 64-char hex hash found in the text."""
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text or "")
    return m.group(1).lower() if m else None


def _ytdlp_expected_sha():
    """Official SHA256 hash of yt-dlp.exe (from the release's SHA2-256SUMS)."""
    try:
        txt = _http_get(YTDLP_SUMS_URL).decode("utf-8", "replace")
        for ln in txt.splitlines():
            parts = ln.split()
            if len(parts) >= 2 and parts[-1].strip().endswith("yt-dlp.exe"):
                return parts[0].strip().lower()
    except Exception:
        return None
    return None


def _stage_ffmpeg():
    """Downloads ffmpeg into _staging. Verifies gyan's hash if available;
    if gyan fails or doesn't verify, falls back to the GitHub (BtbN) build."""
    STAGING_DIR.mkdir(exist_ok=True)
    zip_path = STAGING_DIR / "_ffmpeg.zip"
    ok = False
    # Primary: gyan.dev (+ checksum if it publishes one)
    try:
        _download(FFMPEG_ZIP_URL, zip_path)
        expected = None
        try:
            expected = _parse_sha_text(_http_get(FFMPEG_SHA_URL, timeout=20).decode("utf-8", "replace"))
        except Exception:
            expected = None
        ok = (expected is None) or (_sha256(zip_path) == expected)
    except Exception:
        ok = False
    # Fallback: GitHub (BtbN)
    if not ok:
        zip_path.unlink(missing_ok=True)
        _download(FFMPEG_FALLBACK_URL, zip_path)
    # Extract only the two executables
    try:
        with zipfile.ZipFile(zip_path) as z:
            for member in z.namelist():
                base = member.rsplit("/", 1)[-1]
                if base in ("ffmpeg.exe", "ffprobe.exe"):
                    with z.open(member) as src, open(STAGING_DIR / base, "wb") as out:
                        shutil.copyfileobj(src, out)
    finally:
        zip_path.unlink(missing_ok=True)
    if not (STAGING_DIR / "ffmpeg.exe").exists():
        raise RuntimeError("ffmpeg.exe not found in the zip")


def apply_pending_updates():
    """Runs AT STARTUP, before serving anything: applies what's staged."""
    if not STAGING_DIR.exists():
        return
    staged = _read_json(STAGED_FILE)
    files = [p for p in STAGING_DIR.glob("*.exe")]
    if not files:
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
        return
    try:
        for f in files:
            target = BIN_DIR / f.name
            if target.exists():
                target.unlink()          # delete the old version
            shutil.move(str(f), str(target))   # install the new one
    except PermissionError:
        # some binary is in use: retry on the next startup
        return
    except Exception:
        return
    # record the new versions as installed
    try:
        cur = _read_json(VERSIONS_FILE)
        cur.update(staged.get("versions", {}))
        BIN_DIR.mkdir(exist_ok=True)
        VERSIONS_FILE.write_text(json.dumps(cur, indent=2), "utf-8")
    except Exception:
        pass
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    print("  [update] Dependencies updated to the latest version.")


def check_and_stage_updates():
    """In the background, once started: downloads verified updates to _staging."""
    with update_lock:
        update_state.update({"checking": True, "code": "checking", "notes": []})

    inst = installed_versions()
    already = _read_json(STAGED_FILE).get("versions", {})  # already staged before
    new_versions = dict(already)
    staged_any = bool(already)
    notes = []
    errored = False

    # --- yt-dlp (checksum verification mandatory) ---
    try:
        latest_yt = _latest_ytdlp()
        if latest_yt and latest_yt != inst.get("yt-dlp") and latest_yt != already.get("yt-dlp"):
            expected = _ytdlp_expected_sha()
            if not expected:
                notes.append("ytdlp_nochecksum")
            else:
                STAGING_DIR.mkdir(exist_ok=True)
                dest = STAGING_DIR / "yt-dlp.exe"
                _download(YTDLP_EXE_URL, dest)
                if _sha256(dest) != expected:
                    dest.unlink(missing_ok=True)
                    notes.append("ytdlp_badchecksum")
                else:
                    new_versions["yt-dlp"] = latest_yt
                    staged_any = True
    except Exception:
        errored = True
        notes.append("ytdlp_fail")

    # --- ffmpeg (verified if gyan publishes a hash; otherwise GitHub fallback) ---
    try:
        latest_ff = _latest_ffmpeg()
        inst_ff = inst.get("ffmpeg") or ""
        if latest_ff and latest_ff not in inst_ff and latest_ff != already.get("ffmpeg"):
            _stage_ffmpeg()
            new_versions["ffmpeg"] = latest_ff
            staged_any = True
    except Exception:
        errored = True
        notes.append("ffmpeg_fail")

    # --- Persist the result and set the status code ---
    try:
        if staged_any:
            STAGING_DIR.mkdir(exist_ok=True)
            STAGED_FILE.write_text(
                json.dumps({"versions": new_versions}, indent=2), "utf-8")
            code, pending = "pending", True
        elif errored and not notes_only_soft(notes):
            code, pending = "check_failed", False
        else:
            code, pending = "uptodate", False
        with update_lock:
            update_state.update({"pending": pending, "code": code, "notes": list(notes)})
        # Mark as checked today only if there wasn't a hard error with no result
        if staged_any or not errored:
            mark_checked_today()
    finally:
        with update_lock:
            update_state["checking"] = False


def notes_only_soft(notes):
    """True if the notes are only informational (no hard network failures)."""
    return all(not n.endswith("_fail") for n in notes)


# ---------------------------------------------------------------------------
# Download (subprocess over yt-dlp)
# ---------------------------------------------------------------------------
def _to_num(v):
    if v in (None, "", "NA", "None"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def download_worker(job_id, url, mode, quality, subtitles=None):
    with jobs_lock:
        job = jobs.setdefault(job_id, {})
        job.update({
            "status": "starting", "percent": 0, "mode": mode,
            "cancel": False, "started_at": time.time(),
        })

    outtmpl = str(DOWNLOAD_DIR / "%(title)s.%(ext)s")
    progress_tmpl = (
        "PROG|%(progress.downloaded_bytes)s|%(progress.total_bytes)s"
        "|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s"
    )

    cmd = [
        YTDLP,
        "--newline",
        "--no-playlist",
        "--no-mtime",
        "--no-part",
        "--encoding", "utf-8",
        "-o", outtmpl,
        "--progress-template", progress_tmpl,
    ]
    if FFMPEG_LOCATION:
        cmd += ["--ffmpeg-location", FFMPEG_LOCATION]

    if mode == "audio":
        codec = quality if quality in ("mp3", "m4a", "opus", "wav", "flac") else "mp3"
        cmd += ["-x", "--audio-format", codec, "--audio-quality", "0"]
    else:
        if quality == "best":
            fmt = "bestvideo+bestaudio/best"
        else:
            h = quality.replace("p", "")
            fmt = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        cmd += ["-f", fmt, "--merge-output-format", "mp4"]

    if mode != "audio" and subtitles and subtitles.get("enabled"):
        ui_lang = (subtitles.get("ui_lang") or "en").strip() or "en"
        _lang_map = {"zh": "zh-Hans", "bn": "bn-BD"}
        sub_code = _lang_map.get(ui_lang, ui_lang)
        cmd += [
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", sub_code,
            "--sleep-subtitles", "5",
            "--sleep-requests", "0.75",
            "--retry-sleep", "http:exp=2:60",
        ]

    cmd += [url]

    tail = []  # last lines for the error message
    title = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        with jobs_lock:
            jobs[job_id].update({
                "status": "error",
                "errcode": "ytdlp_missing",
                "error": "yt-dlp.exe not found.",
                "finished_at": time.time(),
            })
        return

    with jobs_lock:
        procs[job_id] = proc

    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        tail.append(line)
        if len(tail) > 20:
            tail.pop(0)

        if line.startswith("PROG|"):
            parts = line.split("|")
            # PROG | downloaded | total | total_est | speed | eta
            downloaded = _to_num(parts[1]) if len(parts) > 1 else None
            total = _to_num(parts[2]) if len(parts) > 2 else None
            total_est = _to_num(parts[3]) if len(parts) > 3 else None
            speed = _to_num(parts[4]) if len(parts) > 4 else None
            eta = _to_num(parts[5]) if len(parts) > 5 else None
            tot = total or total_est
            percent = (downloaded / tot * 100) if (downloaded and tot) else None
            with jobs_lock:
                jobs[job_id].update({
                    "status": "downloading",
                    "percent": round(percent, 1) if percent is not None else None,
                    "downloaded": downloaded,
                    "total": tot,
                    "speed": speed,
                    "eta": int(eta) if eta is not None else None,
                })
        elif "Destination:" in line:
            # [download] Destination: ...  /  [ExtractAudio] Destination: ...
            try:
                title = os.path.basename(line.split("Destination:", 1)[1].strip())
            except Exception:
                pass
        elif "Merging formats into" in line:
            m = re.search(r'Merging formats into "(.+)"', line)
            if m:
                title = os.path.basename(m.group(1))
            with jobs_lock:
                jobs[job_id].update({"status": "processing"})
        elif "[ExtractAudio]" in line or "[VideoConvertor]" in line:
            with jobs_lock:
                jobs[job_id].update({"status": "processing"})

    code = proc.wait()
    with jobs_lock:
        cancelled = jobs.get(job_id, {}).get("cancel")
        procs.pop(job_id, None)

    if cancelled:
        with jobs_lock:
            jobs[job_id].update({
                "status": "cancelled",
                "error": "Download cancelled.",
                "finished_at": time.time(),
            })
    elif code == 0:
        with jobs_lock:
            jobs[job_id].update({
                "status": "done",
                "percent": 100,
                "title": title or "Downloaded file",
                "finished_at": time.time(),
            })
    else:
        # look for an ERROR line in the tail
        err = next((l for l in reversed(tail) if "ERROR" in l), None)
        if not err:
            err = tail[-1] if tail else f"yt-dlp exited with code {code}."
        with jobs_lock:
            jobs[job_id].update({
                "status": "error", "error": err, "finished_at": time.time(),
            })


def queue_worker():
    """Processes the download queue one at a time, in order."""
    while True:
        with queue_cv:
            job_id = None
            while job_id is None:
                for jid in queue_order:
                    j = jobs.get(jid)
                    if j and j.get("status") == "queued":
                        job_id = jid
                        break
                if job_id is None:
                    queue_cv.wait()
            item = dict(jobs[job_id])
        download_worker(job_id, item["url"], item["mode"], item["quality"], item.get("subtitles"))
        with queue_cv:
            queue_cv.notify_all()


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    # Silence the per-request log
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif path == "/api/ping":
            self._send(200, json.dumps({"app": "yt-portable", "version": APP_VERSION}))
        elif path == "/api/queue":
            self._queue_snapshot()
        elif path == "/api/update-status":
            with update_lock:
                self._send(200, json.dumps(update_state))
        elif path == "/api/info":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[-1]) if "?" in self.path else {}
            url = (qs.get("url", [""])[0]).strip()
            if not url:
                self._send(400, json.dumps({"error": "Missing link."}))
            else:
                self._get_info(url)
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/queue":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                data = {}
            self._queue_add(data)
        elif path == "/api/open-folder":
            self._open_folder()
            self._send(200, json.dumps({"ok": True}))
        elif path.startswith("/api/queue/") and path.endswith("/cancel"):
            jid = path.split("/")[-2]
            self._queue_cancel(jid)
        elif path == "/api/quit":
            self._send(200, json.dumps({"ok": True}))
            threading.Thread(
                target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True
            ).start()
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def _queue_add(self, data):
        url = (data.get("url") or "").strip()
        mode = data.get("mode", "video")
        quality = data.get("quality", "best")
        subtitles = data.get("subtitles") if isinstance(data.get("subtitles"), dict) else None
        title = (data.get("title") or "").strip()
        if not url:
            self._send(400, json.dumps({"error": "Missing link."}))
            return
        with queue_cv:
            active = sum(
                1 for jid in queue_order
                if jobs.get(jid, {}).get("status") in QUEUE_ACTIVE_STATUSES
            )
            if active >= MAX_QUEUE:
                self._send(409, json.dumps({"error": "queue_full"}))
                return
            job_id = uuid.uuid4().hex
            jobs[job_id] = {
                "status": "queued", "url": url, "mode": mode, "quality": quality,
                "subtitles": subtitles, "title": title, "percent": None,
                "queued_at": time.time(), "cancel": False,
            }
            queue_order.append(job_id)
            queue_cv.notify_all()
        self._send(200, json.dumps({"job_id": job_id}))

    def _queue_snapshot(self):
        with jobs_lock:
            items = []
            for jid in queue_order:
                j = jobs.get(jid)
                if not j:
                    continue
                items.append({
                    "id": jid, "status": j.get("status"), "url": j.get("url"),
                    "title": j.get("title"), "mode": j.get("mode"),
                    "percent": j.get("percent"), "speed": j.get("speed"),
                    "eta": j.get("eta"), "error": j.get("error"),
                    "errcode": j.get("errcode"),
                })
        self._send(200, json.dumps({"items": items, "max": MAX_QUEUE}))

    def _queue_cancel(self, jid):
        p = None
        with queue_cv:
            job = jobs.get(jid)
            if job is None:
                self._send(404, json.dumps({"error": "not found"}))
                return
            status = job.get("status")
            if status in ("starting", "downloading", "processing"):
                job["cancel"] = True
                p = procs.get(jid)
            else:
                jobs.pop(jid, None)
                if jid in queue_order:
                    queue_order.remove(jid)
            queue_cv.notify_all()
        if p:
            try:
                p.terminate()
            except Exception:
                pass
        self._send(200, json.dumps({"ok": True}))

    def _get_info(self, url):
        cmd = [YTDLP, "-j", "--no-playlist", "--no-warnings", "--skip-download"]
        if FFMPEG_LOCATION:
            cmd += ["--ffmpeg-location", FFMPEG_LOCATION]
        cmd += [url]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                self._send(500, json.dumps({"error": result.stderr[:300]}))
                return
            info = json.loads(result.stdout.splitlines()[0])
            self._send(200, json.dumps({
                "language": info.get("language") or "",
                "title": info.get("title") or "",
                "duration": info.get("duration"),
                "upload_date": info.get("upload_date") or "",
                "channel": info.get("channel") or info.get("uploader") or "",
            }))
        except subprocess.TimeoutExpired:
            self._send(504, json.dumps({"error": "timeout"}))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)[:300]}))

    def _open_folder(self):
        try:
            if os.name == "nt":
                os.startfile(str(DOWNLOAD_DIR))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(DOWNLOAD_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(DOWNLOAD_DIR)])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Interface (embedded HTML + CSS + JS)
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YT Portable</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel-2:#1c232d; --border:#2d333b;
    --text:#e6edf3; --muted:#8b949e; --accent:#ff3b3b; --accent-2:#ff6b6b;
    --ok:#3fb950; --err:#f85149; --radius:14px;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{
    font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
    background:radial-gradient(1200px 600px at 50% -10%,#1b2230,transparent),var(--bg);
    color:var(--text); min-height:100vh; display:flex; align-items:flex-start;
    justify-content:center; padding:48px 16px;
  }
  .card{
    width:100%; max-width:560px; background:var(--panel);
    border:1px solid var(--border); border-radius:var(--radius);
    padding:32px; box-shadow:0 20px 60px rgba(0,0,0,.4);
  }
  .logo{display:flex;align-items:center;gap:12px;margin-bottom:6px}
  .logo .play{
    width:40px;height:40px;border-radius:10px;
    background:linear-gradient(135deg,var(--accent),var(--accent-2));
    display:flex;align-items:center;justify-content:center;
  }
  .logo .play svg{width:18px;height:18px;fill:#fff;margin-left:2px}
  h1{font-size:20px;font-weight:700;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:24px}
  label{display:block;font-size:13px;color:var(--muted);margin-bottom:8px;font-weight:600}
  input[type=text]{
    width:100%;padding:13px 14px;background:var(--panel-2);
    border:1px solid var(--border);border-radius:10px;color:var(--text);
    font-size:14px;outline:none;transition:border .15s;
  }
  input[type=text]:focus{border-color:var(--accent)}
  .row{display:flex;gap:12px;margin-top:20px}
  .row > div{flex:1}
  .seg{display:flex;background:var(--panel-2);border:1px solid var(--border);
    border-radius:10px;padding:4px;gap:4px}
  .seg button{
    flex:1;padding:10px;border:none;background:transparent;color:var(--muted);
    border-radius:7px;font-size:13px;font-weight:600;cursor:pointer;transition:.15s;
  }
  .seg button.active{background:var(--accent);color:#fff}
  select{
    width:100%;padding:11px 12px;background:var(--panel-2);
    border:1px solid var(--border);border-radius:10px;color:var(--text);
    font-size:14px;outline:none;cursor:pointer;
  }
  .go{
    width:100%;margin-top:24px;padding:14px;border:none;border-radius:10px;
    background:linear-gradient(135deg,var(--accent),var(--accent-2));
    color:#fff;font-size:15px;font-weight:700;cursor:pointer;transition:.15s;
  }
  .go:hover{filter:brightness(1.08)}
  .go:disabled{opacity:.5;cursor:not-allowed}
  @keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
  .msg{margin-top:18px;font-size:13px;padding:12px 14px;border-radius:10px;display:none;
    word-break:break-word}
  .msg.ok{display:block;background:rgba(63,185,80,.12);
    border:1px solid rgba(63,185,80,.4);color:var(--ok)}
  .msg.err{display:block;background:rgba(248,81,73,.12);
    border:1px solid rgba(248,81,73,.4);color:var(--err)}
  .foot{margin-top:22px;display:flex;justify-content:space-between;align-items:center}
  .link{background:none;border:none;color:var(--muted);font-size:12px;
    cursor:pointer;text-decoration:underline;padding:0}
  .link:hover{color:var(--text)}
  .pill{font-size:11px;color:var(--muted);background:var(--panel-2);
    border:1px solid var(--border);padding:4px 10px;border-radius:999px}
  .quit-btn{
    display:block;width:100%;margin-top:18px;padding:16px;
    border:1px solid #5a2326;border-radius:10px;
    background:rgba(248,81,73,.14);color:#ff8079;
    font-size:16px;font-weight:700;cursor:pointer;transition:.15s;
  }
  .quit-btn:hover{background:rgba(248,81,73,.24);color:#ffb3ad}
  .topbar{display:flex;align-items:center;justify-content:space-between;gap:12px}
  .lang-wrap{position:relative}
  .lang-btn{display:flex;align-items:center;gap:7px;background:var(--panel-2);
    border:1px solid var(--border);color:var(--text);border-radius:8px;
    padding:7px 10px;font-size:13px;cursor:pointer;max-width:60vw}
  .lang-btn:hover{border-color:#3d4450}
  .lang-btn .caret{color:var(--muted);font-size:10px;margin-left:2px}
  .lang-flag{width:20px;height:14px;border-radius:2px;overflow:hidden;
    display:inline-block;flex:0 0 auto;box-shadow:0 0 0 1px rgba(255,255,255,.08)}
  .lang-flag svg{display:block;width:100%;height:100%}
  .lang-list{position:absolute;top:calc(100% + 6px);right:0;background:var(--panel);
    border:1px solid var(--border);border-radius:10px;padding:6px;max-height:300px;
    overflow:auto;z-index:20;min-width:190px;box-shadow:0 12px 30px rgba(0,0,0,.5);display:none}
  .lang-list.open{display:block}
  .lang-item{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:7px;
    cursor:pointer;font-size:13px;color:var(--text);white-space:nowrap}
  .lang-item:hover,.lang-item.sel{background:var(--panel-2)}
  [dir="rtl"] .lang-list{right:auto;left:0}
  [dir="rtl"] .foot{flex-direction:row-reverse}
  .sub-ctrl{display:flex;align-items:center;gap:12px}
  .tog{display:inline-flex;align-items:center;cursor:pointer;user-select:none;flex:0 0 auto}
  .tog input[type=checkbox]{position:absolute;opacity:0;width:0;height:0}
  .knob{width:36px;height:20px;background:var(--panel-2);border:1px solid var(--border);
    border-radius:10px;position:relative;transition:.2s;flex:0 0 auto}
  .knob::after{content:"";position:absolute;width:14px;height:14px;top:2px;left:2px;
    background:var(--muted);border-radius:50%;transition:.2s}
  .tog input:checked + .knob{background:var(--accent);border-color:var(--accent)}
  .tog input:checked + .knob::after{left:18px;background:#fff}
  #subLang{flex:1}
  .vinfo{margin-top:10px;padding:10px 12px;background:var(--panel-2);
    border:1px solid var(--border);border-radius:10px}
  .vinfo-title{font-size:13px;font-weight:600;color:var(--text);
    overflow:hidden;text-overflow:ellipsis;display:-webkit-box;
    -webkit-line-clamp:2;-webkit-box-orient:vertical}
  .vinfo-meta{display:flex;flex-wrap:wrap;gap:6px 10px;margin-top:6px;
    font-size:12px;color:var(--muted)}
  .vinfo-meta span:empty{display:none}
  .queue-wrap{margin-top:24px}
  .queue-head{display:flex;justify-content:space-between;align-items:center;
    font-size:13px;font-weight:600;color:var(--text);margin-bottom:8px}
  .queue-count{font-size:11px;color:var(--muted);font-weight:400}
  .queue-list{display:flex;flex-direction:column;gap:8px}
  .qitem{padding:10px 12px;background:var(--panel-2);border:1px solid var(--border);
    border-radius:10px}
  .qitem-row{display:flex;justify-content:space-between;align-items:center;gap:8px}
  .qitem-title{font-size:13px;color:var(--text);overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap;flex:1}
  .qitem-x{flex:0 0 auto;background:none;border:none;color:var(--muted);
    cursor:pointer;font-size:14px;line-height:1;padding:4px 7px;border-radius:6px}
  .qitem-x:hover{color:var(--text);background:rgba(255,255,255,.06)}
  .qitem-meta{font-size:11px;color:var(--muted);margin-top:4px}
  .qitem.err .qitem-meta{color:var(--err)}
  .qitem.done .qitem-meta{color:var(--ok)}
  .qitem-bar{height:6px;background:var(--panel);border-radius:999px;overflow:hidden;
    border:1px solid var(--border);margin-top:8px}
  .qitem-bar > i{display:block;height:100%;width:0;border-radius:999px;
    background:linear-gradient(90deg,var(--accent),var(--accent-2));transition:width .3s}
  .qitem-bar.indet > i{width:35%;animation:slide 1.1s infinite ease-in-out}
  [dir="rtl"] .qitem-row{flex-direction:row-reverse}
  [dir="rtl"] .queue-head{flex-direction:row-reverse}
</style>
</head>
<body>
  <div class="card">
    <div class="topbar">
      <div class="logo">
        <div class="play"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></div>
        <div><h1>YT Portable</h1></div>
      </div>
      <div class="lang-wrap" id="langWrap">
        <button class="lang-btn" id="langBtn" type="button" aria-haspopup="listbox" aria-expanded="false">
          <span class="lang-flag" id="langBtnFlag"></span>
          <span id="langBtnName"></span>
          <span class="caret">▾</span>
        </button>
        <div class="lang-list" id="langList" role="listbox"></div>
      </div>
    </div>
    <div class="sub" id="t-sub">Descarga vídeo o audio de YouTube. Todo en local.</div>

    <label for="url" id="t-linkLabel">Enlace</label>
    <input id="url" type="text" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off">

    <div class="vinfo" id="vinfo" style="display:none">
      <div class="vinfo-title" id="vinfoTitle"></div>
      <div class="vinfo-meta">
        <span id="vinfoChannel"></span>
        <span id="vinfoDuration"></span>
        <span id="vinfoDate"></span>
      </div>
    </div>

    <div class="row">
      <div>
        <label id="t-formatLabel">Formato</label>
        <div class="seg" id="mode">
          <button data-mode="video" class="active">🎬 <span id="t-video">Vídeo</span></button>
          <button data-mode="audio">🎵 <span id="t-audioOnly">Solo audio</span></button>
        </div>
      </div>
    </div>

    <div class="row">
      <div>
        <label id="qlabel">Calidad de vídeo</label>
        <select id="quality"></select>
      </div>
    </div>

    <div class="row" id="subRow">
      <div>
        <label id="t-subtitlesLabel">Subtítulos</label>
        <div class="sub-ctrl">
          <label class="tog">
            <input type="checkbox" id="subToggle">
            <span class="knob"></span>
          </label>
          <span id="subInfo" style="display:none;font-size:12px;color:var(--muted)"></span>
        </div>
      </div>
    </div>

    <button class="go" id="go">Descargar</button>

    <div class="msg" id="msg"></div>

    <div class="queue-wrap" id="queueWrap" style="display:none">
      <div class="queue-head">
        <span id="t-queueTitle">Cola de descargas</span>
        <span class="queue-count" id="queueCount"></span>
      </div>
      <div class="queue-list" id="queueList"></div>
    </div>

    <div class="foot">
      <button class="link" id="openFolder">📂 <span id="t-openFolder">Abrir carpeta de descargas</span></button>
      <span class="pill">v__VERSION__ · local</span>
    </div>
    <div id="ustatus" style="margin-top:12px;font-size:11px;color:var(--muted);text-align:center;min-height:14px"></div>
    <button class="quit-btn" id="quit">⏻ <span id="t-closeProgram">Cerrar el programa</span></button>
  </div>

<script>
const I18N = {
  es:{sub:"Descarga vídeo o audio de YouTube. Todo en local.",linkLabel:"Enlace",formatLabel:"Formato",video:"Vídeo",audioOnly:"Solo audio",videoQuality:"Calidad de vídeo",audioFormat:"Formato de audio",qualityBest:"Mejor disponible",download:"Descargar",downloading:"Descargando…",preparing:"Preparando…",cancel:"Cancelar",cancelling:"Cancelando…",convertingAudio:"Convirtiendo audio…",mergingVideo:"Uniendo vídeo y audio…",completed:"Completado",ready:"Listo",cancelledMsg:"Descarga cancelada.",pasteFirst:"Pega un enlace primero.",startError:"Error al iniciar.",unknownError:"Error desconocido",openFolder:"Abrir carpeta de descargas",closeProgram:"Cerrar el programa",closedMsg:"Programa cerrado. Ya puedes cerrar esta pestaña.",u_checking:"Buscando actualizaciones…",u_pending:"Actualización lista: se aplicará al reiniciar.",u_uptodate:"Dependencias al día.",u_checked_today:"Comprobación ya realizada hoy.",u_check_failed:"No se pudo comprobar actualizaciones.",n_ytdlp_nochecksum:"yt-dlp: sin checksum, se omite",n_ytdlp_badchecksum:"yt-dlp: checksum no coincide, descartado",n_ytdlp_fail:"yt-dlp: fallo al comprobar",n_ffmpeg_fail:"ffmpeg: fallo al comprobar",e_ytdlp_missing:"No se encuentra yt-dlp.exe. Vuelve a ejecutar el constructor.",subtitles:"Subtítulos",subtitlesOrig:"Idioma original",subtitlesFetching:"Detectando idioma original…",videoInfoLoading:"Cargando información…",queueTitle:"Cola de descargas",queued:"En cola",queueFull:"Cola llena (máx. 16).",remove:"Quitar"},
  en:{sub:"Download video or audio from YouTube. All local.",linkLabel:"Link",formatLabel:"Format",video:"Video",audioOnly:"Audio only",videoQuality:"Video quality",audioFormat:"Audio format",qualityBest:"Best available",download:"Download",downloading:"Downloading…",preparing:"Preparing…",cancel:"Cancel",cancelling:"Cancelling…",convertingAudio:"Converting audio…",mergingVideo:"Merging video and audio…",completed:"Completed",ready:"Done",cancelledMsg:"Download cancelled.",pasteFirst:"Paste a link first.",startError:"Couldn't start.",unknownError:"Unknown error",openFolder:"Open downloads folder",closeProgram:"Close the program",closedMsg:"Program closed. You can close this tab.",u_checking:"Checking for updates…",u_pending:"Update ready: it will be applied on restart.",u_uptodate:"Dependencies up to date.",u_checked_today:"Already checked today.",u_check_failed:"Couldn't check for updates.",n_ytdlp_nochecksum:"yt-dlp: no checksum, skipped",n_ytdlp_badchecksum:"yt-dlp: checksum mismatch, discarded",n_ytdlp_fail:"yt-dlp: check failed",n_ffmpeg_fail:"ffmpeg: check failed",e_ytdlp_missing:"yt-dlp.exe not found. Run the builder again.",subtitles:"Subtitles",subtitlesOrig:"Original language",subtitlesFetching:"Detecting original language…",videoInfoLoading:"Loading info…",queueTitle:"Download queue",queued:"Queued",queueFull:"Queue full (max. 16).",remove:"Remove"},
  fr:{sub:"Téléchargez une vidéo ou un audio depuis YouTube. Tout en local.",linkLabel:"Lien",formatLabel:"Format",video:"Vidéo",audioOnly:"Audio seul",videoQuality:"Qualité vidéo",audioFormat:"Format audio",qualityBest:"Meilleure disponible",download:"Télécharger",downloading:"Téléchargement…",preparing:"Préparation…",cancel:"Annuler",cancelling:"Annulation…",convertingAudio:"Conversion audio…",mergingVideo:"Fusion vidéo et audio…",completed:"Terminé",ready:"Prêt",cancelledMsg:"Téléchargement annulé.",pasteFirst:"Collez d'abord un lien.",startError:"Impossible de démarrer.",unknownError:"Erreur inconnue",openFolder:"Ouvrir le dossier des téléchargements",closeProgram:"Fermer le programme",closedMsg:"Programme fermé. Vous pouvez fermer cet onglet.",u_checking:"Recherche de mises à jour…",u_pending:"Mise à jour prête : appliquée au redémarrage.",u_uptodate:"Dépendances à jour.",u_checked_today:"Déjà vérifié aujourd'hui.",u_check_failed:"Impossible de vérifier les mises à jour.",n_ytdlp_nochecksum:"yt-dlp : pas de somme de contrôle, ignoré",n_ytdlp_badchecksum:"yt-dlp : somme de contrôle incorrecte, rejeté",n_ytdlp_fail:"yt-dlp : échec de la vérification",n_ffmpeg_fail:"ffmpeg : échec de la vérification",e_ytdlp_missing:"yt-dlp.exe introuvable. Relancez le constructeur.",subtitles:"Sous-titres",subtitlesOrig:"Langue originale",subtitlesFetching:"Détection de la langue d'origine…",videoInfoLoading:"Chargement des informations…",queueTitle:"File de téléchargement",queued:"En attente",queueFull:"File pleine (max. 16).",remove:"Retirer"},
  pt:{sub:"Baixe vídeo ou áudio do YouTube. Tudo local.",linkLabel:"Link",formatLabel:"Formato",video:"Vídeo",audioOnly:"Somente áudio",videoQuality:"Qualidade do vídeo",audioFormat:"Formato de áudio",qualityBest:"Melhor disponível",download:"Baixar",downloading:"Baixando…",preparing:"Preparando…",cancel:"Cancelar",cancelling:"Cancelando…",convertingAudio:"Convertendo áudio…",mergingVideo:"Juntando vídeo e áudio…",completed:"Concluído",ready:"Pronto",cancelledMsg:"Download cancelado.",pasteFirst:"Cole um link primeiro.",startError:"Não foi possível iniciar.",unknownError:"Erro desconhecido",openFolder:"Abrir pasta de downloads",closeProgram:"Fechar o programa",closedMsg:"Programa fechado. Você pode fechar esta aba.",u_checking:"Procurando atualizações…",u_pending:"Atualização pronta: será aplicada ao reiniciar.",u_uptodate:"Dependências atualizadas.",u_checked_today:"Já verificado hoje.",u_check_failed:"Não foi possível verificar atualizações.",n_ytdlp_nochecksum:"yt-dlp: sem checksum, ignorado",n_ytdlp_badchecksum:"yt-dlp: checksum não confere, descartado",n_ytdlp_fail:"yt-dlp: falha ao verificar",n_ffmpeg_fail:"ffmpeg: falha ao verificar",e_ytdlp_missing:"yt-dlp.exe não encontrado. Execute o construtor novamente.",subtitles:"Legendas",subtitlesOrig:"Idioma original",subtitlesFetching:"Detectando idioma original…",videoInfoLoading:"Carregando informações…",queueTitle:"Fila de downloads",queued:"Na fila",queueFull:"Fila cheia (máx. 16).",remove:"Remover"},
  it:{sub:"Scarica video o audio da YouTube. Tutto in locale.",linkLabel:"Link",formatLabel:"Formato",video:"Video",audioOnly:"Solo audio",videoQuality:"Qualità video",audioFormat:"Formato audio",qualityBest:"Migliore disponibile",download:"Scarica",downloading:"Scaricamento…",preparing:"Preparazione…",cancel:"Annulla",cancelling:"Annullamento…",convertingAudio:"Conversione audio…",mergingVideo:"Unione video e audio…",completed:"Completato",ready:"Pronto",cancelledMsg:"Download annullato.",pasteFirst:"Incolla prima un link.",startError:"Impossibile avviare.",unknownError:"Errore sconosciuto",openFolder:"Apri la cartella dei download",closeProgram:"Chiudi il programma",closedMsg:"Programma chiuso. Puoi chiudere questa scheda.",u_checking:"Ricerca aggiornamenti…",u_pending:"Aggiornamento pronto: verrà applicato al riavvio.",u_uptodate:"Dipendenze aggiornate.",u_checked_today:"Già verificato oggi.",u_check_failed:"Impossibile verificare gli aggiornamenti.",n_ytdlp_nochecksum:"yt-dlp: nessun checksum, ignorato",n_ytdlp_badchecksum:"yt-dlp: checksum non corrisponde, scartato",n_ytdlp_fail:"yt-dlp: verifica non riuscita",n_ffmpeg_fail:"ffmpeg: verifica non riuscita",e_ytdlp_missing:"yt-dlp.exe non trovato. Esegui di nuovo il costruttore.",subtitles:"Sottotitoli",subtitlesOrig:"Lingua originale",subtitlesFetching:"Rilevamento della lingua originale…",videoInfoLoading:"Caricamento informazioni…",queueTitle:"Coda di download",queued:"In coda",queueFull:"Coda piena (max. 16).",remove:"Rimuovi"},
  de:{sub:"Video oder Audio von YouTube herunterladen. Alles lokal.",linkLabel:"Link",formatLabel:"Format",video:"Video",audioOnly:"Nur Audio",videoQuality:"Videoqualität",audioFormat:"Audioformat",qualityBest:"Beste verfügbare",download:"Herunterladen",downloading:"Wird heruntergeladen…",preparing:"Vorbereitung…",cancel:"Abbrechen",cancelling:"Wird abgebrochen…",convertingAudio:"Audio wird konvertiert…",mergingVideo:"Video und Audio werden zusammengeführt…",completed:"Fertig",ready:"Fertig",cancelledMsg:"Download abgebrochen.",pasteFirst:"Füge zuerst einen Link ein.",startError:"Konnte nicht starten.",unknownError:"Unbekannter Fehler",openFolder:"Download-Ordner öffnen",closeProgram:"Programm schließen",closedMsg:"Programm geschlossen. Du kannst diesen Tab schließen.",u_checking:"Suche nach Updates…",u_pending:"Update bereit: wird beim Neustart angewendet.",u_uptodate:"Abhängigkeiten aktuell.",u_checked_today:"Heute bereits geprüft.",u_check_failed:"Updates konnten nicht geprüft werden.",n_ytdlp_nochecksum:"yt-dlp: keine Prüfsumme, übersprungen",n_ytdlp_badchecksum:"yt-dlp: Prüfsumme stimmt nicht, verworfen",n_ytdlp_fail:"yt-dlp: Prüfung fehlgeschlagen",n_ffmpeg_fail:"ffmpeg: Prüfung fehlgeschlagen",e_ytdlp_missing:"yt-dlp.exe nicht gefunden. Führe den Builder erneut aus.",subtitles:"Untertitel",subtitlesOrig:"Originalsprache",subtitlesFetching:"Originalsprache wird erkannt…",videoInfoLoading:"Informationen werden geladen…",queueTitle:"Download-Warteschlange",queued:"Wartend",queueFull:"Warteschlange voll (max. 16).",remove:"Entfernen"},
  ru:{sub:"Скачивайте видео или аудио с YouTube. Всё локально.",linkLabel:"Ссылка",formatLabel:"Формат",video:"Видео",audioOnly:"Только аудио",videoQuality:"Качество видео",audioFormat:"Формат аудио",qualityBest:"Наилучшее доступное",download:"Скачать",downloading:"Загрузка…",preparing:"Подготовка…",cancel:"Отмена",cancelling:"Отмена…",convertingAudio:"Конвертация аудио…",mergingVideo:"Объединение видео и аудио…",completed:"Готово",ready:"Готово",cancelledMsg:"Загрузка отменена.",pasteFirst:"Сначала вставьте ссылку.",startError:"Не удалось запустить.",unknownError:"Неизвестная ошибка",openFolder:"Открыть папку загрузок",closeProgram:"Закрыть программу",closedMsg:"Программа закрыта. Можете закрыть эту вкладку.",u_checking:"Проверка обновлений…",u_pending:"Обновление готово: применится при перезапуске.",u_uptodate:"Зависимости актуальны.",u_checked_today:"Сегодня уже проверено.",u_check_failed:"Не удалось проверить обновления.",n_ytdlp_nochecksum:"yt-dlp: нет контрольной суммы, пропущено",n_ytdlp_badchecksum:"yt-dlp: контрольная сумма не совпадает, отклонено",n_ytdlp_fail:"yt-dlp: ошибка проверки",n_ffmpeg_fail:"ffmpeg: ошибка проверки",e_ytdlp_missing:"yt-dlp.exe не найден. Запустите конструктор снова.",subtitles:"Субтитры",subtitlesOrig:"Исходный язык",subtitlesFetching:"Определение исходного языка…",videoInfoLoading:"Загрузка информации…",queueTitle:"Очередь загрузок",queued:"В очереди",queueFull:"Очередь заполнена (макс. 16).",remove:"Удалить"},
  zh:{sub:"从 YouTube 下载视频或音频。全部在本地。",linkLabel:"链接",formatLabel:"格式",video:"视频",audioOnly:"仅音频",videoQuality:"视频质量",audioFormat:"音频格式",qualityBest:"最佳可用",download:"下载",downloading:"下载中…",preparing:"准备中…",cancel:"取消",cancelling:"正在取消…",convertingAudio:"正在转换音频…",mergingVideo:"正在合并视频和音频…",completed:"已完成",ready:"完成",cancelledMsg:"下载已取消。",pasteFirst:"请先粘贴链接。",startError:"无法启动。",unknownError:"未知错误",openFolder:"打开下载文件夹",closeProgram:"关闭程序",closedMsg:"程序已关闭。您可以关闭此标签页。",u_checking:"正在检查更新…",u_pending:"更新已就绪：将在重启后应用。",u_uptodate:"依赖项已是最新。",u_checked_today:"今天已检查。",u_check_failed:"无法检查更新。",n_ytdlp_nochecksum:"yt-dlp：无校验和，已跳过",n_ytdlp_badchecksum:"yt-dlp：校验和不匹配，已丢弃",n_ytdlp_fail:"yt-dlp：检查失败",n_ffmpeg_fail:"ffmpeg：检查失败",e_ytdlp_missing:"找不到 yt-dlp.exe。请重新运行构建器。",subtitles:"字幕",subtitlesOrig:"原始语言",subtitlesFetching:"正在检测原始语言…",videoInfoLoading:"正在加载信息…",queueTitle:"下载队列",queued:"排队中",queueFull:"队列已满（最多 16 个）。",remove:"移除"},
  ja:{sub:"YouTube から動画や音声をダウンロード。すべてローカルで。",linkLabel:"リンク",formatLabel:"形式",video:"動画",audioOnly:"音声のみ",videoQuality:"動画の画質",audioFormat:"音声の形式",qualityBest:"利用可能な最高画質",download:"ダウンロード",downloading:"ダウンロード中…",preparing:"準備中…",cancel:"キャンセル",cancelling:"キャンセル中…",convertingAudio:"音声を変換中…",mergingVideo:"動画と音声を結合中…",completed:"完了",ready:"完了",cancelledMsg:"ダウンロードをキャンセルしました。",pasteFirst:"まずリンクを貼り付けてください。",startError:"開始できませんでした。",unknownError:"不明なエラー",openFolder:"ダウンロードフォルダを開く",closeProgram:"プログラムを終了",closedMsg:"プログラムを終了しました。このタブを閉じてかまいません。",u_checking:"更新を確認中…",u_pending:"更新の準備完了：再起動時に適用されます。",u_uptodate:"依存関係は最新です。",u_checked_today:"本日は確認済みです。",u_check_failed:"更新を確認できませんでした。",n_ytdlp_nochecksum:"yt-dlp: チェックサムなし、スキップ",n_ytdlp_badchecksum:"yt-dlp: チェックサム不一致、破棄",n_ytdlp_fail:"yt-dlp: 確認に失敗",n_ffmpeg_fail:"ffmpeg: 確認に失敗",e_ytdlp_missing:"yt-dlp.exe が見つかりません。ビルダーを再実行してください。",subtitles:"字幕",subtitlesOrig:"元の言語",subtitlesFetching:"元の言語を検出中…",videoInfoLoading:"情報を読み込み中…",queueTitle:"ダウンロードキュー",queued:"待機中",queueFull:"キューが満杯です（最大16件）。",remove:"削除"},
  ko:{sub:"YouTube에서 동영상 또는 오디오를 다운로드합니다. 모두 로컬에서.",linkLabel:"링크",formatLabel:"형식",video:"동영상",audioOnly:"오디오만",videoQuality:"동영상 화질",audioFormat:"오디오 형식",qualityBest:"사용 가능한 최고 화질",download:"다운로드",downloading:"다운로드 중…",preparing:"준비 중…",cancel:"취소",cancelling:"취소 중…",convertingAudio:"오디오 변환 중…",mergingVideo:"동영상과 오디오 병합 중…",completed:"완료",ready:"완료",cancelledMsg:"다운로드가 취소되었습니다.",pasteFirst:"먼저 링크를 붙여넣으세요.",startError:"시작할 수 없습니다.",unknownError:"알 수 없는 오류",openFolder:"다운로드 폴더 열기",closeProgram:"프로그램 닫기",closedMsg:"프로그램이 종료되었습니다. 이 탭을 닫아도 됩니다.",u_checking:"업데이트 확인 중…",u_pending:"업데이트 준비됨: 다시 시작할 때 적용됩니다.",u_uptodate:"종속성이 최신입니다.",u_checked_today:"오늘 이미 확인했습니다.",u_check_failed:"업데이트를 확인할 수 없습니다.",n_ytdlp_nochecksum:"yt-dlp: 체크섬 없음, 건너뜀",n_ytdlp_badchecksum:"yt-dlp: 체크섬 불일치, 폐기됨",n_ytdlp_fail:"yt-dlp: 확인 실패",n_ffmpeg_fail:"ffmpeg: 확인 실패",e_ytdlp_missing:"yt-dlp.exe를 찾을 수 없습니다. 빌더를 다시 실행하세요.",subtitles:"자막",subtitlesOrig:"원래 언어",subtitlesFetching:"원본 언어 감지 중…",videoInfoLoading:"정보를 불러오는 중…",queueTitle:"다운로드 대기열",queued:"대기 중",queueFull:"대기열이 가득 찼습니다 (최대 16개).",remove:"제거"},
  hi:{sub:"YouTube से वीडियो या ऑडियो डाउनलोड करें। सब कुछ लोकल।",linkLabel:"लिंक",formatLabel:"प्रारूप",video:"वीडियो",audioOnly:"केवल ऑडियो",videoQuality:"वीडियो गुणवत्ता",audioFormat:"ऑडियो प्रारूप",qualityBest:"उपलब्ध सर्वोत्तम",download:"डाउनलोड करें",downloading:"डाउनलोड हो रहा है…",preparing:"तैयारी हो रही है…",cancel:"रद्द करें",cancelling:"रद्द किया जा रहा है…",convertingAudio:"ऑडियो परिवर्तित हो रहा है…",mergingVideo:"वीडियो और ऑडियो जोड़े जा रहे हैं…",completed:"पूर्ण",ready:"तैयार",cancelledMsg:"डाउनलोड रद्द किया गया।",pasteFirst:"पहले एक लिंक पेस्ट करें।",startError:"शुरू नहीं हो सका।",unknownError:"अज्ञात त्रुटि",openFolder:"डाउनलोड फ़ोल्डर खोलें",closeProgram:"प्रोग्राम बंद करें",closedMsg:"प्रोग्राम बंद हो गया। आप इस टैब को बंद कर सकते हैं।",u_checking:"अपडेट जाँच रहे हैं…",u_pending:"अपडेट तैयार: पुनः आरंभ पर लागू होगा।",u_uptodate:"निर्भरताएँ अद्यतित हैं।",u_checked_today:"आज पहले ही जाँच हो चुकी है।",u_check_failed:"अपडेट जाँच नहीं सके।",n_ytdlp_nochecksum:"yt-dlp: कोई चेकसम नहीं, छोड़ा गया",n_ytdlp_badchecksum:"yt-dlp: चेकसम मेल नहीं खाता, अस्वीकृत",n_ytdlp_fail:"yt-dlp: जाँच विफल",n_ffmpeg_fail:"ffmpeg: जाँच विफल",e_ytdlp_missing:"yt-dlp.exe नहीं मिला। बिल्डर फिर से चलाएँ।",subtitles:"उपशीर्षक",subtitlesOrig:"मूल भाषा",subtitlesFetching:"मूल भाषा का पता लगाया जा रहा है…",videoInfoLoading:"जानकारी लोड हो रही है…",queueTitle:"डाउनलोड कतार",queued:"कतार में",queueFull:"कतार भर गई है (अधिकतम 16).",remove:"हटाएं"},
  bn:{sub:"YouTube থেকে ভিডিও বা অডিও ডাউনলোড করুন। সবকিছু লোকাল।",linkLabel:"লিঙ্ক",formatLabel:"ফরম্যাট",video:"ভিডিও",audioOnly:"শুধু অডিও",videoQuality:"ভিডিও মান",audioFormat:"অডিও ফরম্যাট",qualityBest:"সেরা উপলব্ধ",download:"ডাউনলোড",downloading:"ডাউনলোড হচ্ছে…",preparing:"প্রস্তুত হচ্ছে…",cancel:"বাতিল",cancelling:"বাতিল করা হচ্ছে…",convertingAudio:"অডিও রূপান্তর হচ্ছে…",mergingVideo:"ভিডিও ও অডিও যুক্ত হচ্ছে…",completed:"সম্পন্ন",ready:"প্রস্তুত",cancelledMsg:"ডাউনলোড বাতিল হয়েছে।",pasteFirst:"প্রথমে একটি লিঙ্ক পেস্ট করুন।",startError:"শুরু করা যায়নি।",unknownError:"অজানা ত্রুটি",openFolder:"ডাউনলোড ফোল্ডার খুলুন",closeProgram:"প্রোগ্রাম বন্ধ করুন",closedMsg:"প্রোগ্রাম বন্ধ হয়েছে। আপনি এই ট্যাবটি বন্ধ করতে পারেন।",u_checking:"আপডেট পরীক্ষা করা হচ্ছে…",u_pending:"আপডেট প্রস্তুত: পুনরায় চালু করলে প্রয়োগ হবে।",u_uptodate:"নির্ভরতাগুলি হালনাগাদ।",u_checked_today:"আজ ইতিমধ্যে পরীক্ষা করা হয়েছে।",u_check_failed:"আপডেট পরীক্ষা করা যায়নি।",n_ytdlp_nochecksum:"yt-dlp: কোনো চেকসাম নেই, বাদ দেওয়া হয়েছে",n_ytdlp_badchecksum:"yt-dlp: চেকসাম মেলেনি, বাতিল",n_ytdlp_fail:"yt-dlp: পরীক্ষা ব্যর্থ",n_ffmpeg_fail:"ffmpeg: পরীক্ষা ব্যর্থ",e_ytdlp_missing:"yt-dlp.exe পাওয়া যায়নি। বিল্ডার আবার চালান।",subtitles:"সাবটাইটেল",subtitlesOrig:"মূল ভাষা",subtitlesFetching:"মূল ভাষা শনাক্ত করা হচ্ছে…",videoInfoLoading:"তথ্য লোড হচ্ছে…",queueTitle:"ডাউনলোড সারি",queued:"সারিতে আছে",queueFull:"সারি পূর্ণ (সর্বোচ্চ ১৬টি)।",remove:"সরান"},
  ar:{sub:"نزّل الفيديو أو الصوت من يوتيوب. كل شيء محلي.",linkLabel:"الرابط",formatLabel:"الصيغة",video:"فيديو",audioOnly:"الصوت فقط",videoQuality:"جودة الفيديو",audioFormat:"صيغة الصوت",qualityBest:"الأفضل المتاح",download:"تنزيل",downloading:"جارٍ التنزيل…",preparing:"جارٍ التحضير…",cancel:"إلغاء",cancelling:"جارٍ الإلغاء…",convertingAudio:"جارٍ تحويل الصوت…",mergingVideo:"جارٍ دمج الفيديو والصوت…",completed:"اكتمل",ready:"تم",cancelledMsg:"تم إلغاء التنزيل.",pasteFirst:"الصق رابطًا أولاً.",startError:"تعذّر البدء.",unknownError:"خطأ غير معروف",openFolder:"فتح مجلد التنزيلات",closeProgram:"إغلاق البرنامج",closedMsg:"تم إغلاق البرنامج. يمكنك إغلاق هذه العلامة.",u_checking:"جارٍ البحث عن تحديثات…",u_pending:"التحديث جاهز: سيُطبَّق عند إعادة التشغيل.",u_uptodate:"التبعيات محدّثة.",u_checked_today:"تم التحقق اليوم بالفعل.",u_check_failed:"تعذّر التحقق من التحديثات.",n_ytdlp_nochecksum:"yt-dlp: لا يوجد تحقق، تم التخطي",n_ytdlp_badchecksum:"yt-dlp: عدم تطابق التحقق، تم الرفض",n_ytdlp_fail:"yt-dlp: فشل التحقق",n_ffmpeg_fail:"ffmpeg: فشل التحقق",e_ytdlp_missing:"تعذّر العثور على yt-dlp.exe. شغّل المُنشئ مرة أخرى.",subtitles:"الترجمات",subtitlesOrig:"اللغة الأصلية",subtitlesFetching:"جارٍ اكتشاف اللغة الأصلية…",videoInfoLoading:"جارٍ تحميل المعلومات…",queueTitle:"قائمة التنزيلات",queued:"في الانتظار",queueFull:"القائمة ممتلئة (الحد الأقصى 16).",remove:"إزالة"},
  id:{sub:"Unduh video atau audio dari YouTube. Semua lokal.",linkLabel:"Tautan",formatLabel:"Format",video:"Video",audioOnly:"Hanya audio",videoQuality:"Kualitas video",audioFormat:"Format audio",qualityBest:"Terbaik yang tersedia",download:"Unduh",downloading:"Mengunduh…",preparing:"Menyiapkan…",cancel:"Batal",cancelling:"Membatalkan…",convertingAudio:"Mengonversi audio…",mergingVideo:"Menggabungkan video dan audio…",completed:"Selesai",ready:"Selesai",cancelledMsg:"Unduhan dibatalkan.",pasteFirst:"Tempel tautan terlebih dahulu.",startError:"Tidak dapat memulai.",unknownError:"Kesalahan tidak diketahui",openFolder:"Buka folder unduhan",closeProgram:"Tutup program",closedMsg:"Program ditutup. Anda dapat menutup tab ini.",u_checking:"Memeriksa pembaruan…",u_pending:"Pembaruan siap: akan diterapkan saat dimulai ulang.",u_uptodate:"Dependensi sudah terbaru.",u_checked_today:"Sudah diperiksa hari ini.",u_check_failed:"Tidak dapat memeriksa pembaruan.",n_ytdlp_nochecksum:"yt-dlp: tanpa checksum, dilewati",n_ytdlp_badchecksum:"yt-dlp: checksum tidak cocok, dibuang",n_ytdlp_fail:"yt-dlp: gagal memeriksa",n_ffmpeg_fail:"ffmpeg: gagal memeriksa",e_ytdlp_missing:"yt-dlp.exe tidak ditemukan. Jalankan builder lagi.",subtitles:"Terjemahan",subtitlesOrig:"Bahasa asli",subtitlesFetching:"Mendeteksi bahasa asli…",videoInfoLoading:"Memuat info…",queueTitle:"Antrean unduhan",queued:"Mengantre",queueFull:"Antrean penuh (maks. 16).",remove:"Hapus"},
  ur:{sub:"یوٹیوب سے ویڈیو یا آڈیو ڈاؤن لوڈ کریں۔ سب کچھ مقامی۔",linkLabel:"لنک",formatLabel:"فارمیٹ",video:"ویڈیو",audioOnly:"صرف آڈیو",videoQuality:"ویڈیو کوالٹی",audioFormat:"آڈیو فارمیٹ",qualityBest:"بہترین دستیاب",download:"ڈاؤن لوڈ",downloading:"ڈاؤن لوڈ ہو رہا ہے…",preparing:"تیاری ہو رہی ہے…",cancel:"منسوخ",cancelling:"منسوخ ہو رہا ہے…",convertingAudio:"آڈیو تبدیل ہو رہا ہے…",mergingVideo:"ویڈیو اور آڈیو ملائے جا رہے ہیں…",completed:"مکمل",ready:"تیار",cancelledMsg:"ڈاؤن لوڈ منسوخ ہو گیا۔",pasteFirst:"پہلے ایک لنک پیسٹ کریں۔",startError:"شروع نہیں ہو سکا۔",unknownError:"نامعلوم خرابی",openFolder:"ڈاؤن لوڈ فولڈر کھولیں",closeProgram:"پروگرام بند کریں",closedMsg:"پروگرام بند ہو گیا۔ آپ یہ ٹیب بند کر سکتے ہیں۔",u_checking:"اپ ڈیٹس چیک ہو رہے ہیں…",u_pending:"اپ ڈیٹ تیار: دوبارہ شروع کرنے پر لاگو ہوگا۔",u_uptodate:"انحصار تازہ ترین ہیں۔",u_checked_today:"آج پہلے ہی چیک ہو چکا ہے۔",u_check_failed:"اپ ڈیٹس چیک نہیں ہو سکے۔",n_ytdlp_nochecksum:"yt-dlp: کوئی چیک سم نہیں، چھوڑ دیا گیا",n_ytdlp_badchecksum:"yt-dlp: چیک سم مطابقت نہیں رکھتا، مسترد",n_ytdlp_fail:"yt-dlp: چیک ناکام",n_ffmpeg_fail:"ffmpeg: چیک ناکام",e_ytdlp_missing:"yt-dlp.exe نہیں ملا۔ بلڈر دوبارہ چلائیں۔",subtitles:"سب ٹائٹل",subtitlesOrig:"اصل زبان",subtitlesFetching:"اصل زبان کا پتہ لگایا جا رہا ہے…",videoInfoLoading:"معلومات لوڈ ہو رہی ہیں…",queueTitle:"ڈاؤن لوڈ قطار",queued:"قطار میں",queueFull:"قطار بھر گئی ہے (زیادہ سے زیادہ 16)۔",remove:"ہٹائیں"},
  cs:{sub:"Stahujte video nebo zvuk z YouTube. Vše lokálně.",linkLabel:"Odkaz",formatLabel:"Formát",video:"Video",audioOnly:"Pouze zvuk",videoQuality:"Kvalita videa",audioFormat:"Formát zvuku",qualityBest:"Nejlepší dostupná",download:"Stáhnout",downloading:"Stahování…",preparing:"Příprava…",cancel:"Zrušit",cancelling:"Rušení…",convertingAudio:"Převod zvuku…",mergingVideo:"Slučování videa a zvuku…",completed:"Hotovo",ready:"Hotovo",cancelledMsg:"Stahování zrušeno.",pasteFirst:"Nejprve vložte odkaz.",startError:"Nelze spustit.",unknownError:"Neznámá chyba",openFolder:"Otevřít složku se staženými soubory",closeProgram:"Zavřít program",closedMsg:"Program zavřen. Tuto kartu můžete zavřít.",u_checking:"Kontrola aktualizací…",u_pending:"Aktualizace připravena: použije se po restartu.",u_uptodate:"Závislosti jsou aktuální.",u_checked_today:"Dnes již zkontrolováno.",u_check_failed:"Nelze zkontrolovat aktualizace.",n_ytdlp_nochecksum:"yt-dlp: bez kontrolního součtu, přeskočeno",n_ytdlp_badchecksum:"yt-dlp: kontrolní součet nesouhlasí, zahozeno",n_ytdlp_fail:"yt-dlp: kontrola selhala",n_ffmpeg_fail:"ffmpeg: kontrola selhala",e_ytdlp_missing:"yt-dlp.exe nenalezen. Spusťte znovu nástroj pro sestavení.",subtitles:"Titulky",subtitlesOrig:"Původní jazyk",subtitlesFetching:"Zjišťování původního jazyka…",videoInfoLoading:"Načítání informací…",queueTitle:"Fronta stahování",queued:"Ve frontě",queueFull:"Fronta je plná (max. 16).",remove:"Odebrat"},
  pl:{sub:"Pobieraj wideo lub audio z YouTube. Wszystko lokalnie.",linkLabel:"Link",formatLabel:"Format",video:"Wideo",audioOnly:"Tylko audio",videoQuality:"Jakość wideo",audioFormat:"Format audio",qualityBest:"Najlepsza dostępna",download:"Pobierz",downloading:"Pobieranie…",preparing:"Przygotowywanie…",cancel:"Anuluj",cancelling:"Anulowanie…",convertingAudio:"Konwertowanie audio…",mergingVideo:"Łączenie wideo i audio…",completed:"Ukończono",ready:"Gotowe",cancelledMsg:"Pobieranie anulowane.",pasteFirst:"Najpierw wklej link.",startError:"Nie można uruchomić.",unknownError:"Nieznany błąd",openFolder:"Otwórz folder pobranych",closeProgram:"Zamknij program",closedMsg:"Program zamknięty. Możesz zamknąć tę kartę.",u_checking:"Sprawdzanie aktualizacji…",u_pending:"Aktualizacja gotowa: zostanie zastosowana po ponownym uruchomieniu.",u_uptodate:"Zależności są aktualne.",u_checked_today:"Już sprawdzono dzisiaj.",u_check_failed:"Nie można sprawdzić aktualizacji.",n_ytdlp_nochecksum:"yt-dlp: brak sumy kontrolnej, pominięto",n_ytdlp_badchecksum:"yt-dlp: suma kontrolna niezgodna, odrzucono",n_ytdlp_fail:"yt-dlp: sprawdzanie nie powiodło się",n_ffmpeg_fail:"ffmpeg: sprawdzanie nie powiodło się",e_ytdlp_missing:"Nie znaleziono yt-dlp.exe. Uruchom ponownie kreator.",subtitles:"Napisy",subtitlesOrig:"Oryginalny język",subtitlesFetching:"Wykrywanie oryginalnego języka…",videoInfoLoading:"Ładowanie informacji…",queueTitle:"Kolejka pobierania",queued:"W kolejce",queueFull:"Kolejka pełna (maks. 16).",remove:"Usuń"}
};
const LANG_NAMES = {es:"Español",en:"English",fr:"Français",pt:"Português",it:"Italiano",de:"Deutsch",ru:"Русский",zh:"中文",ja:"日本語",ko:"한국어",hi:"हिन्दी",bn:"বাংলা",ar:"العربية",id:"Bahasa Indonesia",ur:"اردو",cs:"Čeština",pl:"Polski"};
const FLAGS = {
  es:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#c60b1e"/><rect y="4" width="24" height="8" fill="#ffc400"/></svg>',
  en:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#012169"/><path d="M0,0 24,16 M24,0 0,16" stroke="#fff" stroke-width="3.2"/><path d="M0,0 24,16 M24,0 0,16" stroke="#C8102E" stroke-width="1.8"/><rect x="9.6" width="4.8" height="16" fill="#fff"/><rect y="5.6" width="24" height="4.8" fill="#fff"/><rect x="10.6" width="2.8" height="16" fill="#C8102E"/><rect y="6.6" width="24" height="2.8" fill="#C8102E"/></svg>',
  fr:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#fff"/><rect width="8" height="16" fill="#002395"/><rect x="16" width="8" height="16" fill="#ed2939"/></svg>',
  pt:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#da291c"/><rect width="9.6" height="16" fill="#046a38"/><circle cx="9.6" cy="8" r="2.3" fill="#ffe000" stroke="#fff" stroke-width="0.5"/></svg>',
  it:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#fff"/><rect width="8" height="16" fill="#009246"/><rect x="16" width="8" height="16" fill="#ce2b37"/></svg>',
  de:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#000"/><rect y="5.33" width="24" height="5.33" fill="#dd0000"/><rect y="10.66" width="24" height="5.34" fill="#ffce00"/></svg>',
  ru:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#fff"/><rect y="5.33" width="24" height="5.33" fill="#0039a6"/><rect y="10.66" width="24" height="5.34" fill="#d52b1e"/></svg>',
  zh:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#de2910"/><path fill="#ffde00" d="M5 2.2 6.13 5.68 9.8 5.68 6.83 7.83 7.97 11.3 5 9.16 2.03 11.3 3.17 7.83 0.2 5.68 3.87 5.68Z"/><circle cx="10.5" cy="2.4" r="0.7" fill="#ffde00"/><circle cx="12" cy="4.3" r="0.7" fill="#ffde00"/><circle cx="12" cy="6.8" r="0.7" fill="#ffde00"/><circle cx="10.5" cy="8.6" r="0.7" fill="#ffde00"/></svg>',
  ja:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#fff"/><circle cx="12" cy="8" r="4.8" fill="#bc002d"/></svg>',
  ko:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#fff"/><circle cx="12" cy="8" r="4.4" fill="#cd2e3a"/><path d="M12,3.6 a4.4,4.4 0 0,0 0,8.8 a2.2,2.2 0 0,0 0,-4.4 a2.2,2.2 0 0,1 0,-4.4" fill="#0047a0"/></svg>',
  hi:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#fff"/><rect width="24" height="5.33" fill="#ff9933"/><rect y="10.66" width="24" height="5.34" fill="#138808"/><circle cx="12" cy="8" r="1.9" fill="none" stroke="#000080" stroke-width="0.6"/></svg>',
  bn:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#006a4e"/><circle cx="10.5" cy="8" r="4.2" fill="#f42a41"/></svg>',
  ar:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#165d31"/><rect x="4" y="9.4" width="15" height="1.1" fill="#fff"/><rect x="5" y="6.4" width="3" height="1" fill="#fff"/><rect x="9" y="6.4" width="5" height="1" fill="#fff"/><rect x="15" y="6.4" width="3" height="1" fill="#fff"/></svg>',
  id:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#fff"/><rect width="24" height="8" fill="#ce1126"/></svg>',
  ur:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#01411c"/><rect width="6" height="16" fill="#fff"/><circle cx="15" cy="8" r="3.6" fill="#fff"/><circle cx="16.5" cy="8" r="3" fill="#01411c"/><path fill="#fff" d="M18.2 5.2 18.8 6.8 20.5 6.8 19.1 7.8 19.6 9.5 18.2 8.5 16.8 9.5 17.3 7.8 15.9 6.8 17.6 6.8Z"/></svg>',
  cs:'<svg viewBox="0 0 24 16"><rect width="24" height="8" fill="#fff"/><rect y="8" width="24" height="8" fill="#d7141a"/><path d="M0,0 L12,8 L0,16 Z" fill="#11457e"/></svg>',
  pl:'<svg viewBox="0 0 24 16"><rect width="24" height="16" fill="#dc143c"/><rect width="24" height="8" fill="#fff"/></svg>'
};
const RTL = ["ar","ur"];
let mode = "video";
const $ = s => document.querySelector(s);
const qSel = $("#quality"), qLabel = $("#qlabel");

// --- Idioma ---
function pickLang(){
  let saved = null;
  try{ saved = localStorage.getItem("ytp_lang"); }catch(e){}
  if(saved && I18N[saved]) return saved;
  const nav = (navigator.languages||[navigator.language||"en"]);
  for(const l of nav){ const c=(l||"").slice(0,2).toLowerCase(); if(I18N[c]) return c; }
  return "en";
}
let lang = pickLang();
function t(key){ return (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key; }

const VQ = ["best","1080p","720p","480p","360p"];
const AQ = ["mp3","m4a","opus","wav","flac"];

function fillQuality(){
  qLabel.textContent = mode === "video" ? t("videoQuality") : t("audioFormat");
  const list = mode === "video" ? VQ : AQ;
  qSel.innerHTML = list.map(v =>
    `<option value="${v}">${v==="best" ? t("qualityBest") : v.toUpperCase()}</option>`
  ).join("");
}

function applyLang(){
  document.documentElement.lang = lang;
  document.documentElement.dir = RTL.includes(lang) ? "rtl" : "ltr";
  $("#t-sub").textContent = t("sub");
  $("#t-linkLabel").textContent = t("linkLabel");
  $("#t-formatLabel").textContent = t("formatLabel");
  $("#t-video").textContent = t("video");
  $("#t-audioOnly").textContent = t("audioOnly");
  $("#t-subtitlesLabel").textContent = t("subtitles");
  if(subToggle&&subToggle.checked) updateSubInfo();
  if(vinfo && vinfo.style.display!=="none"){
    const url=$("#url").value.trim();
    if(infoCache.url===url && infoCache.data) renderVinfo(infoCache.data);
  }
  $("#t-openFolder").textContent = t("openFolder");
  $("#t-closeProgram").textContent = t("closeProgram");
  $("#t-queueTitle").textContent = t("queueTitle");
  if(!go.disabled) go.textContent = t("download");
  pollQueue();
  fillQuality();
  renderLangButton();
  document.querySelectorAll(".lang-item").forEach(it =>
    it.classList.toggle("sel", it.dataset.c === lang));
  try{ localStorage.setItem("ytp_lang", lang); }catch(e){}
}

function renderLangButton(){
  $("#langBtnFlag").innerHTML = FLAGS[lang] || "";
  $("#langBtnName").textContent = LANG_NAMES[lang];
}

function closeLang(){
  $("#langList").classList.remove("open");
  $("#langBtn").setAttribute("aria-expanded", "false");
}

function setupLangSelector(){
  const list = $("#langList"), btn = $("#langBtn");
  list.innerHTML = Object.keys(I18N).map(c =>
    `<div class="lang-item${c===lang?' sel':''}" role="option" data-c="${c}">`
    + `<span class="lang-flag">${FLAGS[c]||""}</span><span>${LANG_NAMES[c]}</span></div>`
  ).join("");
  list.querySelectorAll(".lang-item").forEach(it=>{
    it.onclick = ()=>{ lang = it.dataset.c; closeLang(); applyLang(); };
  });
  btn.onclick = (e)=>{
    e.stopPropagation();
    const open = list.classList.toggle("open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  };
  document.addEventListener("click", (e)=>{
    if(!$("#langWrap").contains(e.target)) closeLang();
  });
  document.addEventListener("keydown", (e)=>{ if(e.key==="Escape") closeLang(); });
  renderLangButton();
}

document.querySelectorAll("#mode button").forEach(b=>{
  b.onclick = ()=>{
    document.querySelectorAll("#mode button").forEach(x=>x.classList.remove("active"));
    b.classList.add("active"); mode = b.dataset.mode; fillQuality(); updateSubRow();
  };
});

function fmtBytes(n){ if(!n) return ""; const u=["B","KB","MB","GB"]; let i=0;
  while(n>=1024&&i<u.length-1){n/=1024;i++;} return n.toFixed(1)+" "+u[i]; }
function fmtSpeed(s){ return s ? fmtBytes(s)+"/s" : ""; }
function fmtEta(e){ if(e==null) return ""; const m=Math.floor(e/60), s=e%60;
  return m>0 ? `${m}m ${s}s` : `${s}s`; }

// --- Información del vídeo (compartida entre la ficha de info y los subtítulos) ---
let infoCache={url:null, data:null, promise:null};
function getVideoInfo(url){
  if(infoCache.url===url) return infoCache.promise || Promise.resolve(infoCache.data);
  infoCache={url, data:null, promise:null};
  infoCache.promise = fetch("/api/info?url="+encodeURIComponent(url)).then(r=>r.json()).then(data=>{
    if(infoCache.url===url){ infoCache.data=data; infoCache.promise=null; }
    return data;
  }).catch(e=>{
    if(infoCache.url===url) infoCache={url:null, data:null, promise:null};
    throw e;
  });
  return infoCache.promise;
}

function langDisplayName(code){
  if(!code) return "";
  try{
    const dn=new Intl.DisplayNames([lang,"en"],{type:"language"});
    const name=dn.of(code);
    return name && name.toLowerCase()!==code.toLowerCase() ? `${name} (${code})` : code;
  }catch(e){ return code; }
}

function fmtDuration(sec){
  if(sec==null) return "";
  sec=Math.round(sec);
  const h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60), s=sec%60;
  return h>0 ? `${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}` : `${m}:${String(s).padStart(2,"0")}`;
}

function fmtUploadDate(d){
  if(!d || d.length!==8) return "";
  const dt=new Date(`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}T00:00:00Z`);
  if(isNaN(dt)) return "";
  try{ return dt.toLocaleDateString(lang,{year:"numeric",month:"long",day:"numeric",timeZone:"UTC"}); }
  catch(e){ return d; }
}

// --- Ficha de información del vídeo ---
const vinfo=$("#vinfo"), vinfoTitle=$("#vinfoTitle"), vinfoChannel=$("#vinfoChannel"),
      vinfoDuration=$("#vinfoDuration"), vinfoDate=$("#vinfoDate");
let vinfoReqId=0, vinfoTimer=null;

function renderVinfo(data){
  vinfoTitle.textContent = (data&&data.title) || "";
  vinfoChannel.textContent = (data&&data.channel) || "";
  vinfoDuration.textContent = fmtDuration(data&&data.duration);
  vinfoDate.textContent = fmtUploadDate(data&&data.upload_date);
}

function updateVinfo(){
  const url=$("#url").value.trim();
  if(!url){ vinfo.style.display="none"; return; }
  const reqId=++vinfoReqId;
  vinfo.style.display="";
  vinfoTitle.textContent = t("videoInfoLoading");
  vinfoChannel.textContent=""; vinfoDuration.textContent=""; vinfoDate.textContent="";
  getVideoInfo(url).then(data=>{
    if(reqId!==vinfoReqId || $("#url").value.trim()!==url) return;
    renderVinfo(data);
  }).catch(()=>{
    if(reqId!==vinfoReqId || $("#url").value.trim()!==url) return;
    vinfo.style.display="none";
  });
}

$("#url").addEventListener("input", ()=>{
  clearTimeout(vinfoTimer);
  const url=$("#url").value.trim();
  if(!url){ vinfo.style.display="none"; if(subToggle.checked) updateSubInfo(); return; }
  vinfoTimer=setTimeout(()=>{ updateVinfo(); if(subToggle.checked) updateSubInfo(); }, 600);
});

// --- Subtítulos ---
const subRow=$("#subRow"), subToggle=$("#subToggle"), subInfo=$("#subInfo");
let subLangReqId=0;

function updateSubInfo(){
  subInfo.textContent = t("subtitlesOrig");
  const url=$("#url").value.trim();
  if(!url) return;
  const reqId=++subLangReqId;
  subInfo.textContent = t("subtitlesOrig")+" — "+t("subtitlesFetching");
  getVideoInfo(url).then(data=>{
    if(reqId!==subLangReqId || !subToggle.checked) return;
    const code=(data&&data.language)||"";
    subInfo.textContent = code
      ? t("subtitlesOrig")+": "+langDisplayName(code)
      : t("subtitlesOrig");
  }).catch(()=>{
    if(reqId!==subLangReqId || !subToggle.checked) return;
    subInfo.textContent = t("subtitlesOrig");
  });
}

subToggle.onchange=()=>{
  subInfo.style.display=subToggle.checked?"":"none";
  if(subToggle.checked) updateSubInfo();
};

function updateSubRow(){
  subRow.style.display=mode==="audio"?"none":"";
  if(mode==="audio"&&subToggle.checked){
    subToggle.checked=false; subInfo.style.display="none";
  }
}

updateSubRow();

const go=$("#go"), msg=$("#msg"), queueWrap=$("#queueWrap"), queueList=$("#queueList"),
      queueCount=$("#queueCount");
function setMsg(t,type){ msg.className="msg "+type; msg.textContent=t; }
function clearMsg(){ msg.className="msg"; msg.textContent=""; }
function escHtml(s){ return String(s==null?"":s).replace(/[&<>"']/g, c=>(
  {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }

go.onclick = async ()=>{
  const url = $("#url").value.trim();
  if(!url){ setMsg(t("pasteFirst"),"err"); return; }
  clearMsg();
  try{
    const subtitles = (subToggle.checked && mode!=="audio")
      ? {enabled:true, ui_lang:lang}
      : {enabled:false};
    const cached = (infoCache.url===url && infoCache.data) ? infoCache.data : null;
    const r = await fetch("/api/queue",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({url, mode, quality:qSel.value, subtitles, title:(cached&&cached.title)||""})});
    const data = await r.json();
    if(!r.ok) throw new Error(data.error==="queue_full" ? t("queueFull") : (data.error||t("startError")));
    $("#url").value=""; vinfo.style.display="none";
    pollQueue();
  }catch(e){ setMsg(e.message,"err"); }
};

function qStatusLine(it){
  if(it.status==="queued") return {text:t("queued"), meta:"", percent:null};
  if(it.status==="starting") return {text:t("preparing"), meta:"", percent:null};
  if(it.status==="downloading"){
    const parts=[];
    if(it.percent!=null) parts.push(it.percent+"%");
    if(it.speed) parts.push(fmtSpeed(it.speed));
    if(it.eta!=null) parts.push("ETA "+fmtEta(it.eta));
    return {text:t("downloading"), meta:parts.join("  ·  "), percent:it.percent};
  }
  if(it.status==="processing"){
    return {text: it.mode==="audio" ? t("convertingAudio") : t("mergingVideo"), meta:"", percent:100};
  }
  if(it.status==="done") return {text:"✅ "+t("completed"), meta:"", percent:100, cls:"done"};
  if(it.status==="cancelled") return {text:"⏹️ "+t("cancelledMsg"), meta:"", percent:null, cls:"err"};
  if(it.status==="error"){
    const txt = it.errcode ? t("e_"+it.errcode) : (it.error||t("unknownError"));
    return {text:"❌ "+txt, meta:"", percent:null, cls:"err"};
  }
  return {text:"", meta:"", percent:null};
}

function renderQueue(items, max){
  if(!items.length){ queueWrap.style.display="none"; queueList.innerHTML=""; return; }
  queueWrap.style.display="";
  queueCount.textContent = items.length+"/"+max;
  queueList.innerHTML = items.map(it=>{
    const s = qStatusLine(it);
    const cls = "qitem"+(s.cls?" "+s.cls:"");
    const showBar = it.status==="downloading" || it.status==="processing" || it.status==="starting";
    const indet = it.status==="starting" || (it.status==="downloading" && it.percent==null);
    const barHtml = showBar
      ? '<div class="qitem-bar'+(indet?" indet":"")+'"><i style="width:'+(s.percent!=null?s.percent:0)+'%"></i></div>'
      : "";
    const title = it.title || it.url || "";
    return '<div class="'+cls+'">'
      +'<div class="qitem-row"><span class="qitem-title" title="'+escHtml(title)+'">'+escHtml(title)+'</span>'
      +'<button class="qitem-x" data-id="'+escHtml(it.id)+'" title="'+escHtml(t("remove"))+'">✕</button></div>'
      +'<div class="qitem-meta">'+escHtml(s.text)+(s.meta?"  ·  "+escHtml(s.meta):"")+'</div>'
      +barHtml
      +'</div>';
  }).join("");
  queueList.querySelectorAll(".qitem-x").forEach(btn=>{
    btn.onclick = async ()=>{
      btn.disabled = true;
      try{ await fetch("/api/queue/"+btn.dataset.id+"/cancel",{method:"POST"}); }catch(e){}
      pollQueue();
    };
  });
}

let queuePollTimer = null;
async function pollQueue(){
  try{
    const r = await fetch("/api/queue");
    const data = await r.json();
    renderQueue(data.items||[], data.max||16);
  }catch(e){}
}
queuePollTimer = setInterval(pollQueue, 1000);
pollQueue();

$("#openFolder").onclick = ()=> fetch("/api/open-folder",{method:"POST"});
$("#quit").onclick = async ()=>{
  try{ await fetch("/api/quit",{method:"POST"}); }catch(e){}
  document.body.dir = RTL.includes(lang) ? "rtl" : "ltr";
  document.body.innerHTML =
    '<div style="color:#8b949e;font-family:Segoe UI,sans-serif;text-align:center;'
    +'padding-top:25vh">'+t("closedMsg")+'</div>';
};
$("#url").addEventListener("keydown", e=>{ if(e.key==="Enter") go.click(); });

// Idioma: inicializar selector y aplicar
setupLangSelector();
applyLang();

// Estado de actualización de dependencias (sondeo ligero ~40s)
const ustatus = $("#ustatus");
let upolls = 0;
function updText(u){
  if(u.checking) return "🔄 " + t("u_checking");
  if(!u.code || u.code==="checking") return "";
  let base = t("u_"+u.code);
  const notes = (u.notes||[]).map(n=>t("n_"+n)).filter(Boolean);
  return (u.pending ? "🟢 " : "") + base + (notes.length ? "  ("+notes.join("; ")+")" : "");
}
async function pollUpdate(){
  try{
    const r = await fetch("/api/update-status");
    const u = await r.json();
    ustatus.textContent = updText(u);
    if(!u.checking && u.code && u.code!=="checking") return;
  }catch(e){}
  if(++upolls < 20) setTimeout(pollUpdate, 2000);
}
setTimeout(pollUpdate, 1500);
</script>
</body>
</html>"""
HTML = HTML.replace("__VERSION__", APP_VERSION)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def open_browser(port, delay=1.0):
    time.sleep(delay)
    try:
        webbrowser.open(f"http://{HOST}:{port}")
    except Exception:
        pass


def reaper():
    """Purges finished jobs so that `jobs` doesn't grow without bound."""
    while True:
        time.sleep(120)
        now = time.time()
        with jobs_lock:
            stale = [
                jid for jid, v in jobs.items()
                if v.get("status") in ("done", "error", "cancelled")
                and now - v.get("finished_at", now) > 600
            ]
            for jid in stale:
                jobs.pop(jid, None)
                procs.pop(jid, None)
                if jid in queue_order:
                    queue_order.remove(jid)


def background_update():
    # small delay so it doesn't compete with startup
    time.sleep(2.0)
    if not should_check_today():
        with update_lock:
            update_state.update({"code": "checked_today", "notes": []})
        return
    check_and_stage_updates()


def _setup_output():
    """With pythonw.exe there's no console: stdout/stderr are None and print would
    fail. We redirect to app.log so it doesn't crash and can be diagnosed."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log = open(BASE_DIR / "app.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        log = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def find_running_instance():
    """Looks for a YT Portable instance already listening on the candidate ports.
    Returns the port if found, or None."""
    for p in range(PORT, PORT + 20):
        try:
            req = urllib.request.Request(f"http://{HOST}:{p}/api/ping")
            with urllib.request.urlopen(req, timeout=0.3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("app") == "yt-portable":
                return p
        except Exception:
            continue
    return None


def make_server():
    """Creates the server, trying free ports starting at PORT."""
    last_err = None
    for p in range(PORT, PORT + 20):
        try:
            srv = ThreadingHTTPServer((HOST, p), Handler)
            srv.daemon_threads = True
            return srv, p
        except OSError as e:
            last_err = e
    raise last_err if last_err else OSError("no free ports")


def show_error_page(detail):
    """With no console, open a local page with the error instead of nothing."""
    safe = (str(detail) or "").replace("<", "&lt;").replace(">", "&gt;")
    html = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>YT Portable — error</title></head>"
        "<body style='font-family:Segoe UI,sans-serif;background:#0d1117;"
        "color:#e6edf3;max-width:640px;margin:8vh auto;padding:0 24px;line-height:1.5'>"
        "<h2>YT Portable failed to start</h2>"
        f"<p style='color:#f85149'>{safe}</p>"
        "<p>If the problem persists, check the <code>app.log</code> file "
        "next to the program, or close any other open instances.</p>"
        "</body></html>"
    )
    try:
        p = BASE_DIR / "error.html"
        p.write_text(html, "utf-8")
        webbrowser.open(p.as_uri())
    except Exception:
        pass


def main():
    _setup_output()

    # 1) Apply whatever was staged on the previous run (before serving anything)
    try:
        apply_pending_updates()
    except Exception as e:
        print("  [warn] apply_pending_updates:", e)

    # 2) If an instance is already running, open its UI instead of starting another
    existing_port = find_running_instance()
    if existing_port is not None:
        print(f"  [info] A YT Portable instance is already running on port {existing_port}.")
        print(f"  Opening: http://{HOST}:{existing_port}")
        webbrowser.open(f"http://{HOST}:{existing_port}")
        return

    # 3) Create the server on a free port
    try:
        server, port = make_server()
    except Exception as e:
        msg = ("Couldn't find a free port between "
               f"{PORT} and {PORT + 19}. Another instance may already be running. "
               f"Detail: {e}")
        print("  [error]", msg)
        show_error_page(msg)
        return

    print("=" * 52)
    print("  YT Portable")
    print(f"  Interface: http://{HOST}:{port}")
    print(f"  Downloads: {DOWNLOAD_DIR}")
    if YTDLP == "yt-dlp":
        print("  [warn] bin\\yt-dlp.exe not found (using PATH).")
    if not FFMPEG_LOCATION:
        print("  [warn] bin\\ffmpeg.exe not found (audio/merge may fail).")
    print("  To stop: 'Close the program' button in the interface.")
    print("=" * 52)

    threading.Thread(target=open_browser, args=(port,), daemon=True).start()
    threading.Thread(target=background_update, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=queue_worker, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("  [error] server:", e)
        show_error_page(f"The server stopped: {e}")


if __name__ == "__main__":
    main()
