#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YT Portable — Descargador de vídeo/audio de YouTube (Windows, autocontenido).

No requiere instalar nada: usa el Python embebido de la carpeta `runtime\\`,
el `yt-dlp.exe` y el `ffmpeg.exe` de la carpeta `bin\\`.
Solo usa la librería estándar de Python (sin Flask ni pip).
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
# Rutas y configuración
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
BIN_DIR = BASE_DIR / "bin"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

APP_VERSION = "0.8"

HOST = "127.0.0.1"
PORT = 8765

# Localizar yt-dlp (preferimos el .exe local; si no, el del PATH)
_ytdlp_local = BIN_DIR / "yt-dlp.exe"
YTDLP = str(_ytdlp_local) if _ytdlp_local.exists() else "yt-dlp"

# Localizar ffmpeg (carpeta para pasarle a yt-dlp con --ffmpeg-location)
FFMPEG_LOCATION = str(BIN_DIR) if (BIN_DIR / "ffmpeg.exe").exists() else None

# Evitar ventanas de consola emergentes en Windows
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# --- Autoactualización de dependencias ---
STAGING_DIR = BASE_DIR / "_staging"          # área de preparación (próximo arranque)
VERSIONS_FILE = BIN_DIR / "versions.json"    # versiones instaladas
STAGED_FILE = STAGING_DIR / "staged.json"    # versiones preparadas
CHECK_FILE = BIN_DIR / "update_check.json"   # fecha de la última comprobación

YTDLP_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
YTDLP_EXE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YTDLP_SUMS_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
FFMPEG_VER_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_SHA_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"
# Fuente alternativa si gyan.dev no responde (GitHub, builds de BtbN):
FFMPEG_FALLBACK_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-win64-gpl.zip"
)

# Estado de actualización (lo lee la interfaz; usa códigos, no texto)
update_state = {"checking": False, "pending": False, "code": "", "notes": []}
update_lock = threading.Lock()

# Almacén de trabajos y procesos en curso
jobs = {}
procs = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Autoactualización de dependencias (staged: aplica en el próximo arranque)
# ---------------------------------------------------------------------------
def _ssl_ctx():
    """Contexto TLS que confía en el almacén de certificados de Windows."""
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
    """True si hoy aún no se ha comprobado actualizaciones."""
    return _read_json(CHECK_FILE).get("last") != _today()


def mark_checked_today():
    try:
        BIN_DIR.mkdir(exist_ok=True)
        CHECK_FILE.write_text(json.dumps({"last": _today()}), "utf-8")
    except Exception:
        pass


def installed_versions():
    """Versiones instaladas: del versions.json o preguntando a los binarios."""
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
    """Devuelve el primer hash hex de 64 chars que aparezca en el texto."""
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text or "")
    return m.group(1).lower() if m else None


def _ytdlp_expected_sha():
    """Hash SHA256 oficial de yt-dlp.exe (de SHA2-256SUMS de la release)."""
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
    """Descarga ffmpeg a _staging. Verifica el hash de gyan si está disponible;
    si gyan falla o no verifica, usa el build de GitHub (BtbN) como respaldo."""
    STAGING_DIR.mkdir(exist_ok=True)
    zip_path = STAGING_DIR / "_ffmpeg.zip"
    ok = False
    # Primario: gyan.dev (+ checksum si lo publica)
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
    # Respaldo: GitHub (BtbN)
    if not ok:
        zip_path.unlink(missing_ok=True)
        _download(FFMPEG_FALLBACK_URL, zip_path)
    # Extraer solo los dos ejecutables
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
        raise RuntimeError("ffmpeg.exe no encontrado en el zip")


def apply_pending_updates():
    """Se ejecuta AL ARRANCAR, antes de servir nada: aplica lo preparado."""
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
                target.unlink()          # borra la versión vieja
            shutil.move(str(f), str(target))   # instala la nueva
    except PermissionError:
        # algún binario está en uso: se reintenta en el próximo arranque
        return
    except Exception:
        return
    # registrar las versiones nuevas como instaladas
    try:
        cur = _read_json(VERSIONS_FILE)
        cur.update(staged.get("versions", {}))
        BIN_DIR.mkdir(exist_ok=True)
        VERSIONS_FILE.write_text(json.dumps(cur, indent=2), "utf-8")
    except Exception:
        pass
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    print("  [update] Dependencias actualizadas a la última versión.")


def check_and_stage_updates():
    """En segundo plano, ya arrancado: descarga novedades verificadas a _staging."""
    with update_lock:
        update_state.update({"checking": True, "code": "checking", "notes": []})

    inst = installed_versions()
    already = _read_json(STAGED_FILE).get("versions", {})  # ya preparado antes
    new_versions = dict(already)
    staged_any = bool(already)
    notes = []
    errored = False

    # --- yt-dlp (con verificación de checksum obligatoria) ---
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

    # --- ffmpeg (verificación si gyan publica hash; si no, respaldo GitHub) ---
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

    # --- Persistir resultado y fijar código de estado ---
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
        # Marcar como comprobado hoy solo si no hubo error duro sin resultado
        if staged_any or not errored:
            mark_checked_today()
    finally:
        with update_lock:
            update_state["checking"] = False


def notes_only_soft(notes):
    """True si las notas son solo informativas (no fallos de red duros)."""
    return all(not n.endswith("_fail") for n in notes)


# ---------------------------------------------------------------------------
# Descarga (subprocess sobre yt-dlp)
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
        jobs[job_id] = {
            "status": "starting", "percent": 0, "mode": mode,
            "cancel": False, "created_at": time.time(),
        }

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
        sub_lang = (subtitles.get("lang") or "orig").strip() or "orig"
        cmd += [
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", sub_lang,
        ]

    cmd += [url]

    tail = []  # últimas líneas para mensaje de error
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
                "error": "Descarga cancelada.",
                "finished_at": time.time(),
            })
    elif code == 0:
        with jobs_lock:
            jobs[job_id].update({
                "status": "done",
                "percent": 100,
                "title": title or "Archivo descargado",
                "finished_at": time.time(),
            })
    else:
        # buscar una línea de ERROR en la cola
        err = next((l for l in reversed(tail) if "ERROR" in l), None)
        if not err:
            err = tail[-1] if tail else f"yt-dlp terminó con código {code}."
        with jobs_lock:
            jobs[job_id].update({
                "status": "error", "error": err, "finished_at": time.time(),
            })


# ---------------------------------------------------------------------------
# Servidor HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    # Silenciar el log por petición
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
        elif path.startswith("/api/progress/"):
            self._sse(path.rsplit("/", 1)[-1])
        elif path == "/api/update-status":
            with update_lock:
                self._send(200, json.dumps(update_state))
        elif path == "/api/info":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[-1]) if "?" in self.path else {}
            url = (qs.get("url", [""])[0]).strip()
            if not url:
                self._send(400, json.dumps({"error": "Falta el enlace."}))
            else:
                self._get_info(url)
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/download":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                data = {}
            url = (data.get("url") or "").strip()
            mode = data.get("mode", "video")
            quality = data.get("quality", "best")
            subtitles = data.get("subtitles") if isinstance(data.get("subtitles"), dict) else None
            if not url:
                self._send(400, json.dumps({"error": "Falta el enlace."}))
                return
            job_id = uuid.uuid4().hex
            threading.Thread(
                target=download_worker,
                args=(job_id, url, mode, quality, subtitles),
                daemon=True,
            ).start()
            self._send(200, json.dumps({"job_id": job_id}))
        elif path == "/api/open-folder":
            self._open_folder()
            self._send(200, json.dumps({"ok": True}))
        elif path.startswith("/api/cancel/"):
            jid = path.rsplit("/", 1)[-1]
            with jobs_lock:
                p = procs.get(jid)
                if jid in jobs:
                    jobs[jid]["cancel"] = True
            if p:
                try:
                    p.terminate()
                except Exception:
                    pass
            self._send(200, json.dumps({"ok": bool(p)}))
        elif path == "/api/quit":
            self._send(200, json.dumps({"ok": True}))
            threading.Thread(
                target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True
            ).start()
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def _sse(self, job_id):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = None
        try:
            while True:
                with jobs_lock:
                    job = jobs.get(job_id)
                    snap = dict(job) if job else {"status": "unknown"}
                payload = json.dumps(snap)
                if payload != last:
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last = payload
                if snap.get("status") in ("done", "error", "cancelled", "unknown"):
                    break
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
            subs = list(info.get("subtitles", {}).keys())
            auto = list(info.get("automatic_captions", {}).keys())
            self._send(200, json.dumps({"subtitles": subs, "automatic_captions": auto}))
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
# Interfaz (HTML + CSS + JS embebidos)
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
  .progress-wrap{margin-top:24px;display:none}
  .bar{height:10px;background:var(--panel-2);border-radius:999px;overflow:hidden;
    border:1px solid var(--border)}
  .bar > i{display:block;height:100%;width:0;border-radius:999px;
    background:linear-gradient(90deg,var(--accent),var(--accent-2));transition:width .3s}
  .bar.indet > i{width:35%;animation:slide 1.1s infinite ease-in-out}
  @keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
  .pstats{display:flex;justify-content:space-between;font-size:12px;
    color:var(--muted);margin-top:10px}
  .cancel-btn{margin-top:12px;width:100%;padding:9px;border:1px solid var(--border);
    border-radius:8px;background:var(--panel-2);color:var(--muted);font-size:13px;
    font-weight:600;cursor:pointer;transition:.15s}
  .cancel-btn:hover{color:var(--text);border-color:#5a2326}
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
  [dir="rtl"] .pstats{flex-direction:row-reverse}
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
          <select id="subLang" style="display:none"></select>
        </div>
      </div>
    </div>

    <button class="go" id="go">Descargar</button>

    <div class="progress-wrap" id="pwrap">
      <div class="bar" id="bar"><i id="pbar"></i></div>
      <div class="pstats">
        <span id="pname">Preparando…</span>
        <span id="pmeta"></span>
      </div>
      <button class="cancel-btn" id="cancel">Cancelar</button>
    </div>

    <div class="msg" id="msg"></div>

    <div class="foot">
      <button class="link" id="openFolder">📂 <span id="t-openFolder">Abrir carpeta de descargas</span></button>
      <span class="pill">v__VERSION__ · local</span>
    </div>
    <div id="ustatus" style="margin-top:12px;font-size:11px;color:var(--muted);text-align:center;min-height:14px"></div>
    <button class="quit-btn" id="quit">⏻ <span id="t-closeProgram">Cerrar el programa</span></button>
  </div>

<script>
const I18N = {
  es:{sub:"Descarga vídeo o audio de YouTube. Todo en local.",linkLabel:"Enlace",formatLabel:"Formato",video:"Vídeo",audioOnly:"Solo audio",videoQuality:"Calidad de vídeo",audioFormat:"Formato de audio",qualityBest:"Mejor disponible",download:"Descargar",downloading:"Descargando…",preparing:"Preparando…",cancel:"Cancelar",cancelling:"Cancelando…",convertingAudio:"Convirtiendo audio…",mergingVideo:"Uniendo vídeo y audio…",completed:"Completado",ready:"Listo",cancelledMsg:"Descarga cancelada.",pasteFirst:"Pega un enlace primero.",startError:"Error al iniciar.",unknownError:"Error desconocido",openFolder:"Abrir carpeta de descargas",closeProgram:"Cerrar el programa",closedMsg:"Programa cerrado. Ya puedes cerrar esta pestaña.",u_checking:"Buscando actualizaciones…",u_pending:"Actualización lista: se aplicará al reiniciar.",u_uptodate:"Dependencias al día.",u_checked_today:"Comprobación ya realizada hoy.",u_check_failed:"No se pudo comprobar actualizaciones.",n_ytdlp_nochecksum:"yt-dlp: sin checksum, se omite",n_ytdlp_badchecksum:"yt-dlp: checksum no coincide, descartado",n_ytdlp_fail:"yt-dlp: fallo al comprobar",n_ffmpeg_fail:"ffmpeg: fallo al comprobar",e_ytdlp_missing:"No se encuentra yt-dlp.exe. Vuelve a ejecutar el constructor.",subtitles:"Subtítulos",subtitlesOrig:"Idioma original",subtitlesFetching:"Buscando idiomas…"},
  en:{sub:"Download video or audio from YouTube. All local.",linkLabel:"Link",formatLabel:"Format",video:"Video",audioOnly:"Audio only",videoQuality:"Video quality",audioFormat:"Audio format",qualityBest:"Best available",download:"Download",downloading:"Downloading…",preparing:"Preparing…",cancel:"Cancel",cancelling:"Cancelling…",convertingAudio:"Converting audio…",mergingVideo:"Merging video and audio…",completed:"Completed",ready:"Done",cancelledMsg:"Download cancelled.",pasteFirst:"Paste a link first.",startError:"Couldn't start.",unknownError:"Unknown error",openFolder:"Open downloads folder",closeProgram:"Close the program",closedMsg:"Program closed. You can close this tab.",u_checking:"Checking for updates…",u_pending:"Update ready: it will be applied on restart.",u_uptodate:"Dependencies up to date.",u_checked_today:"Already checked today.",u_check_failed:"Couldn't check for updates.",n_ytdlp_nochecksum:"yt-dlp: no checksum, skipped",n_ytdlp_badchecksum:"yt-dlp: checksum mismatch, discarded",n_ytdlp_fail:"yt-dlp: check failed",n_ffmpeg_fail:"ffmpeg: check failed",e_ytdlp_missing:"yt-dlp.exe not found. Run the builder again.",subtitles:"Subtitles",subtitlesOrig:"Original language",subtitlesFetching:"Fetching languages…"},
  fr:{sub:"Téléchargez une vidéo ou un audio depuis YouTube. Tout en local.",linkLabel:"Lien",formatLabel:"Format",video:"Vidéo",audioOnly:"Audio seul",videoQuality:"Qualité vidéo",audioFormat:"Format audio",qualityBest:"Meilleure disponible",download:"Télécharger",downloading:"Téléchargement…",preparing:"Préparation…",cancel:"Annuler",cancelling:"Annulation…",convertingAudio:"Conversion audio…",mergingVideo:"Fusion vidéo et audio…",completed:"Terminé",ready:"Prêt",cancelledMsg:"Téléchargement annulé.",pasteFirst:"Collez d'abord un lien.",startError:"Impossible de démarrer.",unknownError:"Erreur inconnue",openFolder:"Ouvrir le dossier des téléchargements",closeProgram:"Fermer le programme",closedMsg:"Programme fermé. Vous pouvez fermer cet onglet.",u_checking:"Recherche de mises à jour…",u_pending:"Mise à jour prête : appliquée au redémarrage.",u_uptodate:"Dépendances à jour.",u_checked_today:"Déjà vérifié aujourd'hui.",u_check_failed:"Impossible de vérifier les mises à jour.",n_ytdlp_nochecksum:"yt-dlp : pas de somme de contrôle, ignoré",n_ytdlp_badchecksum:"yt-dlp : somme de contrôle incorrecte, rejeté",n_ytdlp_fail:"yt-dlp : échec de la vérification",n_ffmpeg_fail:"ffmpeg : échec de la vérification",e_ytdlp_missing:"yt-dlp.exe introuvable. Relancez le constructeur.",subtitles:"Sous-titres",subtitlesOrig:"Langue originale",subtitlesFetching:"Recherche des langues…"},
  pt:{sub:"Baixe vídeo ou áudio do YouTube. Tudo local.",linkLabel:"Link",formatLabel:"Formato",video:"Vídeo",audioOnly:"Somente áudio",videoQuality:"Qualidade do vídeo",audioFormat:"Formato de áudio",qualityBest:"Melhor disponível",download:"Baixar",downloading:"Baixando…",preparing:"Preparando…",cancel:"Cancelar",cancelling:"Cancelando…",convertingAudio:"Convertendo áudio…",mergingVideo:"Juntando vídeo e áudio…",completed:"Concluído",ready:"Pronto",cancelledMsg:"Download cancelado.",pasteFirst:"Cole um link primeiro.",startError:"Não foi possível iniciar.",unknownError:"Erro desconhecido",openFolder:"Abrir pasta de downloads",closeProgram:"Fechar o programa",closedMsg:"Programa fechado. Você pode fechar esta aba.",u_checking:"Procurando atualizações…",u_pending:"Atualização pronta: será aplicada ao reiniciar.",u_uptodate:"Dependências atualizadas.",u_checked_today:"Já verificado hoje.",u_check_failed:"Não foi possível verificar atualizações.",n_ytdlp_nochecksum:"yt-dlp: sem checksum, ignorado",n_ytdlp_badchecksum:"yt-dlp: checksum não confere, descartado",n_ytdlp_fail:"yt-dlp: falha ao verificar",n_ffmpeg_fail:"ffmpeg: falha ao verificar",e_ytdlp_missing:"yt-dlp.exe não encontrado. Execute o construtor novamente.",subtitles:"Legendas",subtitlesOrig:"Idioma original",subtitlesFetching:"Buscando idiomas…"},
  it:{sub:"Scarica video o audio da YouTube. Tutto in locale.",linkLabel:"Link",formatLabel:"Formato",video:"Video",audioOnly:"Solo audio",videoQuality:"Qualità video",audioFormat:"Formato audio",qualityBest:"Migliore disponibile",download:"Scarica",downloading:"Scaricamento…",preparing:"Preparazione…",cancel:"Annulla",cancelling:"Annullamento…",convertingAudio:"Conversione audio…",mergingVideo:"Unione video e audio…",completed:"Completato",ready:"Pronto",cancelledMsg:"Download annullato.",pasteFirst:"Incolla prima un link.",startError:"Impossibile avviare.",unknownError:"Errore sconosciuto",openFolder:"Apri la cartella dei download",closeProgram:"Chiudi il programma",closedMsg:"Programma chiuso. Puoi chiudere questa scheda.",u_checking:"Ricerca aggiornamenti…",u_pending:"Aggiornamento pronto: verrà applicato al riavvio.",u_uptodate:"Dipendenze aggiornate.",u_checked_today:"Già verificato oggi.",u_check_failed:"Impossibile verificare gli aggiornamenti.",n_ytdlp_nochecksum:"yt-dlp: nessun checksum, ignorato",n_ytdlp_badchecksum:"yt-dlp: checksum non corrisponde, scartato",n_ytdlp_fail:"yt-dlp: verifica non riuscita",n_ffmpeg_fail:"ffmpeg: verifica non riuscita",e_ytdlp_missing:"yt-dlp.exe non trovato. Esegui di nuovo il costruttore.",subtitles:"Sottotitoli",subtitlesOrig:"Lingua originale",subtitlesFetching:"Ricerca lingue…"},
  de:{sub:"Video oder Audio von YouTube herunterladen. Alles lokal.",linkLabel:"Link",formatLabel:"Format",video:"Video",audioOnly:"Nur Audio",videoQuality:"Videoqualität",audioFormat:"Audioformat",qualityBest:"Beste verfügbare",download:"Herunterladen",downloading:"Wird heruntergeladen…",preparing:"Vorbereitung…",cancel:"Abbrechen",cancelling:"Wird abgebrochen…",convertingAudio:"Audio wird konvertiert…",mergingVideo:"Video und Audio werden zusammengeführt…",completed:"Fertig",ready:"Fertig",cancelledMsg:"Download abgebrochen.",pasteFirst:"Füge zuerst einen Link ein.",startError:"Konnte nicht starten.",unknownError:"Unbekannter Fehler",openFolder:"Download-Ordner öffnen",closeProgram:"Programm schließen",closedMsg:"Programm geschlossen. Du kannst diesen Tab schließen.",u_checking:"Suche nach Updates…",u_pending:"Update bereit: wird beim Neustart angewendet.",u_uptodate:"Abhängigkeiten aktuell.",u_checked_today:"Heute bereits geprüft.",u_check_failed:"Updates konnten nicht geprüft werden.",n_ytdlp_nochecksum:"yt-dlp: keine Prüfsumme, übersprungen",n_ytdlp_badchecksum:"yt-dlp: Prüfsumme stimmt nicht, verworfen",n_ytdlp_fail:"yt-dlp: Prüfung fehlgeschlagen",n_ffmpeg_fail:"ffmpeg: Prüfung fehlgeschlagen",e_ytdlp_missing:"yt-dlp.exe nicht gefunden. Führe den Builder erneut aus.",subtitles:"Untertitel",subtitlesOrig:"Originalsprache",subtitlesFetching:"Sprachen suchen…"},
  ru:{sub:"Скачивайте видео или аудио с YouTube. Всё локально.",linkLabel:"Ссылка",formatLabel:"Формат",video:"Видео",audioOnly:"Только аудио",videoQuality:"Качество видео",audioFormat:"Формат аудио",qualityBest:"Наилучшее доступное",download:"Скачать",downloading:"Загрузка…",preparing:"Подготовка…",cancel:"Отмена",cancelling:"Отмена…",convertingAudio:"Конвертация аудио…",mergingVideo:"Объединение видео и аудио…",completed:"Готово",ready:"Готово",cancelledMsg:"Загрузка отменена.",pasteFirst:"Сначала вставьте ссылку.",startError:"Не удалось запустить.",unknownError:"Неизвестная ошибка",openFolder:"Открыть папку загрузок",closeProgram:"Закрыть программу",closedMsg:"Программа закрыта. Можете закрыть эту вкладку.",u_checking:"Проверка обновлений…",u_pending:"Обновление готово: применится при перезапуске.",u_uptodate:"Зависимости актуальны.",u_checked_today:"Сегодня уже проверено.",u_check_failed:"Не удалось проверить обновления.",n_ytdlp_nochecksum:"yt-dlp: нет контрольной суммы, пропущено",n_ytdlp_badchecksum:"yt-dlp: контрольная сумма не совпадает, отклонено",n_ytdlp_fail:"yt-dlp: ошибка проверки",n_ffmpeg_fail:"ffmpeg: ошибка проверки",e_ytdlp_missing:"yt-dlp.exe не найден. Запустите конструктор снова.",subtitles:"Субтитры",subtitlesOrig:"Исходный язык",subtitlesFetching:"Поиск языков…"},
  zh:{sub:"从 YouTube 下载视频或音频。全部在本地。",linkLabel:"链接",formatLabel:"格式",video:"视频",audioOnly:"仅音频",videoQuality:"视频质量",audioFormat:"音频格式",qualityBest:"最佳可用",download:"下载",downloading:"下载中…",preparing:"准备中…",cancel:"取消",cancelling:"正在取消…",convertingAudio:"正在转换音频…",mergingVideo:"正在合并视频和音频…",completed:"已完成",ready:"完成",cancelledMsg:"下载已取消。",pasteFirst:"请先粘贴链接。",startError:"无法启动。",unknownError:"未知错误",openFolder:"打开下载文件夹",closeProgram:"关闭程序",closedMsg:"程序已关闭。您可以关闭此标签页。",u_checking:"正在检查更新…",u_pending:"更新已就绪：将在重启后应用。",u_uptodate:"依赖项已是最新。",u_checked_today:"今天已检查。",u_check_failed:"无法检查更新。",n_ytdlp_nochecksum:"yt-dlp：无校验和，已跳过",n_ytdlp_badchecksum:"yt-dlp：校验和不匹配，已丢弃",n_ytdlp_fail:"yt-dlp：检查失败",n_ffmpeg_fail:"ffmpeg：检查失败",e_ytdlp_missing:"找不到 yt-dlp.exe。请重新运行构建器。",subtitles:"字幕",subtitlesOrig:"原始语言",subtitlesFetching:"正在获取语言…"},
  ja:{sub:"YouTube から動画や音声をダウンロード。すべてローカルで。",linkLabel:"リンク",formatLabel:"形式",video:"動画",audioOnly:"音声のみ",videoQuality:"動画の画質",audioFormat:"音声の形式",qualityBest:"利用可能な最高画質",download:"ダウンロード",downloading:"ダウンロード中…",preparing:"準備中…",cancel:"キャンセル",cancelling:"キャンセル中…",convertingAudio:"音声を変換中…",mergingVideo:"動画と音声を結合中…",completed:"完了",ready:"完了",cancelledMsg:"ダウンロードをキャンセルしました。",pasteFirst:"まずリンクを貼り付けてください。",startError:"開始できませんでした。",unknownError:"不明なエラー",openFolder:"ダウンロードフォルダを開く",closeProgram:"プログラムを終了",closedMsg:"プログラムを終了しました。このタブを閉じてかまいません。",u_checking:"更新を確認中…",u_pending:"更新の準備完了：再起動時に適用されます。",u_uptodate:"依存関係は最新です。",u_checked_today:"本日は確認済みです。",u_check_failed:"更新を確認できませんでした。",n_ytdlp_nochecksum:"yt-dlp: チェックサムなし、スキップ",n_ytdlp_badchecksum:"yt-dlp: チェックサム不一致、破棄",n_ytdlp_fail:"yt-dlp: 確認に失敗",n_ffmpeg_fail:"ffmpeg: 確認に失敗",e_ytdlp_missing:"yt-dlp.exe が見つかりません。ビルダーを再実行してください。",subtitles:"字幕",subtitlesOrig:"元の言語",subtitlesFetching:"言語を取得中…"},
  ko:{sub:"YouTube에서 동영상 또는 오디오를 다운로드합니다. 모두 로컬에서.",linkLabel:"링크",formatLabel:"형식",video:"동영상",audioOnly:"오디오만",videoQuality:"동영상 화질",audioFormat:"오디오 형식",qualityBest:"사용 가능한 최고 화질",download:"다운로드",downloading:"다운로드 중…",preparing:"준비 중…",cancel:"취소",cancelling:"취소 중…",convertingAudio:"오디오 변환 중…",mergingVideo:"동영상과 오디오 병합 중…",completed:"완료",ready:"완료",cancelledMsg:"다운로드가 취소되었습니다.",pasteFirst:"먼저 링크를 붙여넣으세요.",startError:"시작할 수 없습니다.",unknownError:"알 수 없는 오류",openFolder:"다운로드 폴더 열기",closeProgram:"프로그램 닫기",closedMsg:"프로그램이 종료되었습니다. 이 탭을 닫아도 됩니다.",u_checking:"업데이트 확인 중…",u_pending:"업데이트 준비됨: 다시 시작할 때 적용됩니다.",u_uptodate:"종속성이 최신입니다.",u_checked_today:"오늘 이미 확인했습니다.",u_check_failed:"업데이트를 확인할 수 없습니다.",n_ytdlp_nochecksum:"yt-dlp: 체크섬 없음, 건너뜀",n_ytdlp_badchecksum:"yt-dlp: 체크섬 불일치, 폐기됨",n_ytdlp_fail:"yt-dlp: 확인 실패",n_ffmpeg_fail:"ffmpeg: 확인 실패",e_ytdlp_missing:"yt-dlp.exe를 찾을 수 없습니다. 빌더를 다시 실행하세요.",subtitles:"자막",subtitlesOrig:"원래 언어",subtitlesFetching:"언어를 가져오는 중…"},
  hi:{sub:"YouTube से वीडियो या ऑडियो डाउनलोड करें। सब कुछ लोकल।",linkLabel:"लिंक",formatLabel:"प्रारूप",video:"वीडियो",audioOnly:"केवल ऑडियो",videoQuality:"वीडियो गुणवत्ता",audioFormat:"ऑडियो प्रारूप",qualityBest:"उपलब्ध सर्वोत्तम",download:"डाउनलोड करें",downloading:"डाउनलोड हो रहा है…",preparing:"तैयारी हो रही है…",cancel:"रद्द करें",cancelling:"रद्द किया जा रहा है…",convertingAudio:"ऑडियो परिवर्तित हो रहा है…",mergingVideo:"वीडियो और ऑडियो जोड़े जा रहे हैं…",completed:"पूर्ण",ready:"तैयार",cancelledMsg:"डाउनलोड रद्द किया गया।",pasteFirst:"पहले एक लिंक पेस्ट करें।",startError:"शुरू नहीं हो सका।",unknownError:"अज्ञात त्रुटि",openFolder:"डाउनलोड फ़ोल्डर खोलें",closeProgram:"प्रोग्राम बंद करें",closedMsg:"प्रोग्राम बंद हो गया। आप इस टैब को बंद कर सकते हैं।",u_checking:"अपडेट जाँच रहे हैं…",u_pending:"अपडेट तैयार: पुनः आरंभ पर लागू होगा।",u_uptodate:"निर्भरताएँ अद्यतित हैं।",u_checked_today:"आज पहले ही जाँच हो चुकी है।",u_check_failed:"अपडेट जाँच नहीं सके।",n_ytdlp_nochecksum:"yt-dlp: कोई चेकसम नहीं, छोड़ा गया",n_ytdlp_badchecksum:"yt-dlp: चेकसम मेल नहीं खाता, अस्वीकृत",n_ytdlp_fail:"yt-dlp: जाँच विफल",n_ffmpeg_fail:"ffmpeg: जाँच विफल",e_ytdlp_missing:"yt-dlp.exe नहीं मिला। बिल्डर फिर से चलाएँ।",subtitles:"उपशीर्षक",subtitlesOrig:"मूल भाषा",subtitlesFetching:"भाषाएं खोज रहे हैं…"},
  bn:{sub:"YouTube থেকে ভিডিও বা অডিও ডাউনলোড করুন। সবকিছু লোকাল।",linkLabel:"লিঙ্ক",formatLabel:"ফরম্যাট",video:"ভিডিও",audioOnly:"শুধু অডিও",videoQuality:"ভিডিও মান",audioFormat:"অডিও ফরম্যাট",qualityBest:"সেরা উপলব্ধ",download:"ডাউনলোড",downloading:"ডাউনলোড হচ্ছে…",preparing:"প্রস্তুত হচ্ছে…",cancel:"বাতিল",cancelling:"বাতিল করা হচ্ছে…",convertingAudio:"অডিও রূপান্তর হচ্ছে…",mergingVideo:"ভিডিও ও অডিও যুক্ত হচ্ছে…",completed:"সম্পন্ন",ready:"প্রস্তুত",cancelledMsg:"ডাউনলোড বাতিল হয়েছে।",pasteFirst:"প্রথমে একটি লিঙ্ক পেস্ট করুন।",startError:"শুরু করা যায়নি।",unknownError:"অজানা ত্রুটি",openFolder:"ডাউনলোড ফোল্ডার খুলুন",closeProgram:"প্রোগ্রাম বন্ধ করুন",closedMsg:"প্রোগ্রাম বন্ধ হয়েছে। আপনি এই ট্যাবটি বন্ধ করতে পারেন।",u_checking:"আপডেট পরীক্ষা করা হচ্ছে…",u_pending:"আপডেট প্রস্তুত: পুনরায় চালু করলে প্রয়োগ হবে।",u_uptodate:"নির্ভরতাগুলি হালনাগাদ।",u_checked_today:"আজ ইতিমধ্যে পরীক্ষা করা হয়েছে।",u_check_failed:"আপডেট পরীক্ষা করা যায়নি।",n_ytdlp_nochecksum:"yt-dlp: কোনো চেকসাম নেই, বাদ দেওয়া হয়েছে",n_ytdlp_badchecksum:"yt-dlp: চেকসাম মেলেনি, বাতিল",n_ytdlp_fail:"yt-dlp: পরীক্ষা ব্যর্থ",n_ffmpeg_fail:"ffmpeg: পরীক্ষা ব্যর্থ",e_ytdlp_missing:"yt-dlp.exe পাওয়া যায়নি। বিল্ডার আবার চালান।",subtitles:"সাবটাইটেল",subtitlesOrig:"মূল ভাষা",subtitlesFetching:"ভাষা খোঁজা হচ্ছে…"},
  ar:{sub:"نزّل الفيديو أو الصوت من يوتيوب. كل شيء محلي.",linkLabel:"الرابط",formatLabel:"الصيغة",video:"فيديو",audioOnly:"الصوت فقط",videoQuality:"جودة الفيديو",audioFormat:"صيغة الصوت",qualityBest:"الأفضل المتاح",download:"تنزيل",downloading:"جارٍ التنزيل…",preparing:"جارٍ التحضير…",cancel:"إلغاء",cancelling:"جارٍ الإلغاء…",convertingAudio:"جارٍ تحويل الصوت…",mergingVideo:"جارٍ دمج الفيديو والصوت…",completed:"اكتمل",ready:"تم",cancelledMsg:"تم إلغاء التنزيل.",pasteFirst:"الصق رابطًا أولاً.",startError:"تعذّر البدء.",unknownError:"خطأ غير معروف",openFolder:"فتح مجلد التنزيلات",closeProgram:"إغلاق البرنامج",closedMsg:"تم إغلاق البرنامج. يمكنك إغلاق هذه العلامة.",u_checking:"جارٍ البحث عن تحديثات…",u_pending:"التحديث جاهز: سيُطبَّق عند إعادة التشغيل.",u_uptodate:"التبعيات محدّثة.",u_checked_today:"تم التحقق اليوم بالفعل.",u_check_failed:"تعذّر التحقق من التحديثات.",n_ytdlp_nochecksum:"yt-dlp: لا يوجد تحقق، تم التخطي",n_ytdlp_badchecksum:"yt-dlp: عدم تطابق التحقق، تم الرفض",n_ytdlp_fail:"yt-dlp: فشل التحقق",n_ffmpeg_fail:"ffmpeg: فشل التحقق",e_ytdlp_missing:"تعذّر العثور على yt-dlp.exe. شغّل المُنشئ مرة أخرى.",subtitles:"الترجمات",subtitlesOrig:"اللغة الأصلية",subtitlesFetching:"جارٍ البحث عن اللغات…"},
  id:{sub:"Unduh video atau audio dari YouTube. Semua lokal.",linkLabel:"Tautan",formatLabel:"Format",video:"Video",audioOnly:"Hanya audio",videoQuality:"Kualitas video",audioFormat:"Format audio",qualityBest:"Terbaik yang tersedia",download:"Unduh",downloading:"Mengunduh…",preparing:"Menyiapkan…",cancel:"Batal",cancelling:"Membatalkan…",convertingAudio:"Mengonversi audio…",mergingVideo:"Menggabungkan video dan audio…",completed:"Selesai",ready:"Selesai",cancelledMsg:"Unduhan dibatalkan.",pasteFirst:"Tempel tautan terlebih dahulu.",startError:"Tidak dapat memulai.",unknownError:"Kesalahan tidak diketahui",openFolder:"Buka folder unduhan",closeProgram:"Tutup program",closedMsg:"Program ditutup. Anda dapat menutup tab ini.",u_checking:"Memeriksa pembaruan…",u_pending:"Pembaruan siap: akan diterapkan saat dimulai ulang.",u_uptodate:"Dependensi sudah terbaru.",u_checked_today:"Sudah diperiksa hari ini.",u_check_failed:"Tidak dapat memeriksa pembaruan.",n_ytdlp_nochecksum:"yt-dlp: tanpa checksum, dilewati",n_ytdlp_badchecksum:"yt-dlp: checksum tidak cocok, dibuang",n_ytdlp_fail:"yt-dlp: gagal memeriksa",n_ffmpeg_fail:"ffmpeg: gagal memeriksa",e_ytdlp_missing:"yt-dlp.exe tidak ditemukan. Jalankan builder lagi.",subtitles:"Terjemahan",subtitlesOrig:"Bahasa asli",subtitlesFetching:"Mengambil bahasa…"},
  ur:{sub:"یوٹیوب سے ویڈیو یا آڈیو ڈاؤن لوڈ کریں۔ سب کچھ مقامی۔",linkLabel:"لنک",formatLabel:"فارمیٹ",video:"ویڈیو",audioOnly:"صرف آڈیو",videoQuality:"ویڈیو کوالٹی",audioFormat:"آڈیو فارمیٹ",qualityBest:"بہترین دستیاب",download:"ڈاؤن لوڈ",downloading:"ڈاؤن لوڈ ہو رہا ہے…",preparing:"تیاری ہو رہی ہے…",cancel:"منسوخ",cancelling:"منسوخ ہو رہا ہے…",convertingAudio:"آڈیو تبدیل ہو رہا ہے…",mergingVideo:"ویڈیو اور آڈیو ملائے جا رہے ہیں…",completed:"مکمل",ready:"تیار",cancelledMsg:"ڈاؤن لوڈ منسوخ ہو گیا۔",pasteFirst:"پہلے ایک لنک پیسٹ کریں۔",startError:"شروع نہیں ہو سکا۔",unknownError:"نامعلوم خرابی",openFolder:"ڈاؤن لوڈ فولڈر کھولیں",closeProgram:"پروگرام بند کریں",closedMsg:"پروگرام بند ہو گیا۔ آپ یہ ٹیب بند کر سکتے ہیں۔",u_checking:"اپ ڈیٹس چیک ہو رہے ہیں…",u_pending:"اپ ڈیٹ تیار: دوبارہ شروع کرنے پر لاگو ہوگا۔",u_uptodate:"انحصار تازہ ترین ہیں۔",u_checked_today:"آج پہلے ہی چیک ہو چکا ہے۔",u_check_failed:"اپ ڈیٹس چیک نہیں ہو سکے۔",n_ytdlp_nochecksum:"yt-dlp: کوئی چیک سم نہیں، چھوڑ دیا گیا",n_ytdlp_badchecksum:"yt-dlp: چیک سم مطابقت نہیں رکھتا، مسترد",n_ytdlp_fail:"yt-dlp: چیک ناکام",n_ffmpeg_fail:"ffmpeg: چیک ناکام",e_ytdlp_missing:"yt-dlp.exe نہیں ملا۔ بلڈر دوبارہ چلائیں۔",subtitles:"سب ٹائٹل",subtitlesOrig:"اصل زبان",subtitlesFetching:"زبانیں تلاش ہو رہی ہیں…"},
  cs:{sub:"Stahujte video nebo zvuk z YouTube. Vše lokálně.",linkLabel:"Odkaz",formatLabel:"Formát",video:"Video",audioOnly:"Pouze zvuk",videoQuality:"Kvalita videa",audioFormat:"Formát zvuku",qualityBest:"Nejlepší dostupná",download:"Stáhnout",downloading:"Stahování…",preparing:"Příprava…",cancel:"Zrušit",cancelling:"Rušení…",convertingAudio:"Převod zvuku…",mergingVideo:"Slučování videa a zvuku…",completed:"Hotovo",ready:"Hotovo",cancelledMsg:"Stahování zrušeno.",pasteFirst:"Nejprve vložte odkaz.",startError:"Nelze spustit.",unknownError:"Neznámá chyba",openFolder:"Otevřít složku se staženými soubory",closeProgram:"Zavřít program",closedMsg:"Program zavřen. Tuto kartu můžete zavřít.",u_checking:"Kontrola aktualizací…",u_pending:"Aktualizace připravena: použije se po restartu.",u_uptodate:"Závislosti jsou aktuální.",u_checked_today:"Dnes již zkontrolováno.",u_check_failed:"Nelze zkontrolovat aktualizace.",n_ytdlp_nochecksum:"yt-dlp: bez kontrolního součtu, přeskočeno",n_ytdlp_badchecksum:"yt-dlp: kontrolní součet nesouhlasí, zahozeno",n_ytdlp_fail:"yt-dlp: kontrola selhala",n_ffmpeg_fail:"ffmpeg: kontrola selhala",e_ytdlp_missing:"yt-dlp.exe nenalezen. Spusťte znovu nástroj pro sestavení.",subtitles:"Titulky",subtitlesOrig:"Původní jazyk",subtitlesFetching:"Hledání jazyků…"},
  pl:{sub:"Pobieraj wideo lub audio z YouTube. Wszystko lokalnie.",linkLabel:"Link",formatLabel:"Format",video:"Wideo",audioOnly:"Tylko audio",videoQuality:"Jakość wideo",audioFormat:"Format audio",qualityBest:"Najlepsza dostępna",download:"Pobierz",downloading:"Pobieranie…",preparing:"Przygotowywanie…",cancel:"Anuluj",cancelling:"Anulowanie…",convertingAudio:"Konwertowanie audio…",mergingVideo:"Łączenie wideo i audio…",completed:"Ukończono",ready:"Gotowe",cancelledMsg:"Pobieranie anulowane.",pasteFirst:"Najpierw wklej link.",startError:"Nie można uruchomić.",unknownError:"Nieznany błąd",openFolder:"Otwórz folder pobranych",closeProgram:"Zamknij program",closedMsg:"Program zamknięty. Możesz zamknąć tę kartę.",u_checking:"Sprawdzanie aktualizacji…",u_pending:"Aktualizacja gotowa: zostanie zastosowana po ponownym uruchomieniu.",u_uptodate:"Zależności są aktualne.",u_checked_today:"Już sprawdzono dzisiaj.",u_check_failed:"Nie można sprawdzić aktualizacji.",n_ytdlp_nochecksum:"yt-dlp: brak sumy kontrolnej, pominięto",n_ytdlp_badchecksum:"yt-dlp: suma kontrolna niezgodna, odrzucono",n_ytdlp_fail:"yt-dlp: sprawdzanie nie powiodło się",n_ffmpeg_fail:"ffmpeg: sprawdzanie nie powiodło się",e_ytdlp_missing:"Nie znaleziono yt-dlp.exe. Uruchom ponownie kreator.",subtitles:"Napisy",subtitlesOrig:"Oryginalny język",subtitlesFetching:"Pobieranie języków…"}
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
  if(subLang.options[0]&&subLang.options[0].value==="orig") subLang.options[0].textContent=t("subtitlesOrig");
  $("#t-openFolder").textContent = t("openFolder");
  $("#t-closeProgram").textContent = t("closeProgram");
  if(!go.disabled) go.textContent = t("download");
  cancelBtn.textContent = t("cancel");
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

// --- Subtítulos ---
const subRow=$("#subRow"), subToggle=$("#subToggle"), subLang=$("#subLang");
let subFetchCtrl=null, subLangsCache=null;

function populateSubLang(langs){
  const origOpt=`<option value="orig">${t("subtitlesOrig")}</option>`;
  if(!langs||langs.length===0){ subLang.innerHTML=origOpt; return; }
  const all=[...new Set(langs)].sort();
  subLang.innerHTML=origOpt+all.map(l=>`<option value="${l}">${l}</option>`).join("");
}

async function fetchSubLangs(){
  const url=$("#url").value.trim();
  if(!url||!subToggle.checked) return;
  subLang.disabled=true;
  subLang.innerHTML=`<option>${t("subtitlesFetching")}</option>`;
  subLangsCache=null;
  if(subFetchCtrl) subFetchCtrl.abort();
  subFetchCtrl=new AbortController();
  try{
    const r=await fetch("/api/info?url="+encodeURIComponent(url),{signal:subFetchCtrl.signal});
    if(!r.ok) throw new Error();
    const data=await r.json();
    const langs=[...new Set([...(data.subtitles||[]),...(data.automatic_captions||[])])];
    subLangsCache=langs;
    populateSubLang(langs);
  }catch(e){
    if(e.name!=="AbortError") populateSubLang([]);
  }finally{
    subLang.disabled=false;
  }
}

subToggle.onchange=()=>{
  subLang.style.display=subToggle.checked?"":"none";
  if(subToggle.checked&&!subLangsCache) fetchSubLangs();
};

function updateSubRow(){
  subRow.style.display=mode==="audio"?"none":"";
  if(mode==="audio"&&subToggle.checked){
    subToggle.checked=false; subLang.style.display="none";
  }
}

populateSubLang([]);
updateSubRow();

const go=$("#go"), msg=$("#msg"), pwrap=$("#pwrap"), bar=$("#bar"),
      pbar=$("#pbar"), pname=$("#pname"), pmeta=$("#pmeta"), cancelBtn=$("#cancel");
function setMsg(t,type){ msg.className="msg "+type; msg.textContent=t; }
function clearMsg(){ msg.className="msg"; msg.textContent=""; }
function resetBtn(){ go.disabled=false; go.textContent=t("download"); }

let currentJob = null;
cancelBtn.onclick = async ()=>{
  if(!currentJob) return;
  cancelBtn.disabled = true; cancelBtn.textContent = t("cancelling");
  try{ await fetch("/api/cancel/"+currentJob,{method:"POST"}); }catch(e){}
};

go.onclick = async ()=>{
  const url = $("#url").value.trim();
  if(!url){ setMsg(t("pasteFirst"),"err"); return; }
  clearMsg();
  go.disabled=true; go.textContent=t("downloading");
  pwrap.style.display="block"; pbar.style.width="0%"; bar.classList.remove("indet");
  pname.textContent=t("preparing"); pmeta.textContent="";
  cancelBtn.disabled=false; cancelBtn.textContent=t("cancel");

  let job;
  try{
    const subtitles = (subToggle.checked && mode!=="audio")
      ? {enabled:true, lang:subLang.value||"orig"}
      : {enabled:false};
    const r = await fetch("/api/download",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({url, mode, quality:qSel.value, subtitles})});
    const data = await r.json();
    if(!r.ok) throw new Error(data.error||t("startError"));
    job = data.job_id; currentJob = job;
  }catch(e){ setMsg(e.message,"err"); resetBtn(); return; }

  const es = new EventSource("/api/progress/"+job);
  es.onmessage = (ev)=>{
    const d = JSON.parse(ev.data);
    if(d.status==="downloading"){
      if(d.percent!=null){ bar.classList.remove("indet"); pbar.style.width=d.percent+"%"; }
      else { bar.classList.add("indet"); }
      pname.textContent=t("downloading");
      const parts=[];
      if(d.percent!=null) parts.push(d.percent+"%");
      if(d.speed) parts.push(fmtSpeed(d.speed));
      if(d.eta!=null) parts.push("ETA "+fmtEta(d.eta));
      pmeta.textContent=parts.join("  ·  ");
    } else if(d.status==="processing"){
      bar.classList.remove("indet"); pbar.style.width="100%";
      pname.textContent = mode==="audio" ? t("convertingAudio") : t("mergingVideo");
      pmeta.textContent="";
    } else if(d.status==="done"){
      bar.classList.remove("indet"); pbar.style.width="100%";
      pname.textContent=t("completed"); pmeta.textContent="";
      setMsg("✅ "+t("ready")+": "+(d.title||""),"ok");
      es.close(); currentJob=null; pwrap.style.display="none"; resetBtn();
    } else if(d.status==="cancelled"){
      setMsg("⏹️ "+t("cancelledMsg"),"err");
      es.close(); currentJob=null; pwrap.style.display="none"; resetBtn();
    } else if(d.status==="error"){
      const txt = d.errcode ? t("e_"+d.errcode) : (d.error||t("unknownError"));
      setMsg("❌ "+txt,"err");
      es.close(); currentJob=null; pwrap.style.display="none"; resetBtn();
    } else if(d.status==="unknown"){ es.close(); currentJob=null; resetBtn(); }
  };
  es.onerror = ()=>{ es.close(); resetBtn(); };
};

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
# Arranque
# ---------------------------------------------------------------------------
def open_browser(port, delay=1.0):
    time.sleep(delay)
    try:
        webbrowser.open(f"http://{HOST}:{port}")
    except Exception:
        pass


def reaper():
    """Purga trabajos terminados para que `jobs` no crezca sin límite."""
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


def background_update():
    # pequeño retardo para no competir con el arranque
    time.sleep(2.0)
    if not should_check_today():
        with update_lock:
            update_state.update({"code": "checked_today", "notes": []})
        return
    check_and_stage_updates()


def _setup_output():
    """Con pythonw.exe no hay consola: stdout/stderr son None y print fallaría.
    Redirigimos a app.log para no romper y poder diagnosticar."""
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


def make_server():
    """Crea el servidor probando puertos libres a partir de PORT."""
    last_err = None
    for p in range(PORT, PORT + 20):
        try:
            srv = ThreadingHTTPServer((HOST, p), Handler)
            srv.daemon_threads = True
            return srv, p
        except OSError as e:
            last_err = e
    raise last_err if last_err else OSError("sin puertos libres")


def show_error_page(detail):
    """Sin consola, abre una página local con el error en vez del vacío."""
    safe = (str(detail) or "").replace("<", "&lt;").replace(">", "&gt;")
    html = (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<title>YT Portable — error</title></head>"
        "<body style='font-family:Segoe UI,sans-serif;background:#0d1117;"
        "color:#e6edf3;max-width:640px;margin:8vh auto;padding:0 24px;line-height:1.5'>"
        "<h2>YT Portable no pudo arrancar</h2>"
        f"<p style='color:#f85149'>{safe}</p>"
        "<p>Si el problema persiste, revisa el archivo <code>app.log</code> "
        "que está junto al programa, o cierra otras instancias abiertas.</p>"
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

    # 1) Aplicar lo preparado en el arranque anterior (antes de servir nada)
    try:
        apply_pending_updates()
    except Exception as e:
        print("  [warn] apply_pending_updates:", e)

    # 2) Crear el servidor en un puerto libre
    try:
        server, port = make_server()
    except Exception as e:
        msg = ("No se encontró ningún puerto libre entre "
               f"{PORT} y {PORT + 19}. Puede que ya haya otra instancia abierta. "
               f"Detalle: {e}")
        print("  [error]", msg)
        show_error_page(msg)
        return

    print("=" * 52)
    print("  YT Portable")
    print(f"  Interfaz:  http://{HOST}:{port}")
    print(f"  Descargas: {DOWNLOAD_DIR}")
    if YTDLP == "yt-dlp":
        print("  [aviso] No se encontró bin\\yt-dlp.exe (usando PATH).")
    if not FFMPEG_LOCATION:
        print("  [aviso] No se encontró bin\\ffmpeg.exe (audio/merge pueden fallar).")
    print("  Para detener: botón 'Cerrar el programa' en la interfaz.")
    print("=" * 52)

    threading.Thread(target=open_browser, args=(port,), daemon=True).start()
    threading.Thread(target=background_update, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("  [error] servidor:", e)
        show_error_page(f"El servidor se detuvo: {e}")


if __name__ == "__main__":
    main()
