"""BunkrDownloader WebUI - Flask app wrapping the BunkrDownloader CLI."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

DATA_DIR = Path("/data")
CONFIG_DIR = DATA_DIR / "config"
DOWNLOADS_DIR = DATA_DIR / "downloads"
LOGS_DIR = DATA_DIR / "logs"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
URLS_FILE = CONFIG_DIR / "URLs.txt"
LOG_FILE = LOGS_DIR / "session.log"

# ── defaults ──
DEFAULT_SETTINGS = {
    "concurrency": 3,
    "maxRetries": 5,
    "downloadPath": "/data/downloads",
    "downloadSubdir": "",
    "ignoreList": "",
    "includeList": "",
    "httpProxy": "",
    "httpsProxy": "",
}

# ── runtime / download job state ──
download_process = None
download_lock = threading.Lock()
download_running = False
MAX_OUTPUT_LINES = 300

# Parsed job state
job_state = {
    "phase": "idle",       # idle | starting | running | stopping
    "urlCount": 0,
    "urlIndex": 0,
    "urlLabel": "",
    "files": [],
    "logs": [],
    "summary": None,
    "startTime": None,
    "endTime": None,
}


def reset_job():
    global job_state
    job_state = {
        "phase": "idle",
        "urlCount": 0,
        "urlIndex": 0,
        "urlLabel": "",
        "files": [],
        "logs": [],
        "summary": None,
        "startTime": None,
        "endTime": None,
    }


def load_settings():
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        pass
    return {**DEFAULT_SETTINGS}


def save_settings(s):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def read_logs(tail=200):
    if not LOG_FILE.exists():
        return ""
    lines = LOG_FILE.read_text(errors="replace").splitlines()
    return "\n".join(lines[-tail:])


def list_downloads():
    dl = DOWNLOADS_DIR
    if not dl.exists():
        return []
    result = []
    for item in sorted(dl.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if item.is_dir():
            files = []
            total_size = 0
            for f in item.rglob("*"):
                if f.is_file():
                    sz = f.stat().st_size
                    files.append({"name": str(f.relative_to(item)), "size": sz})
                    total_size += sz
            result.append({
                "name": item.name,
                "type": "album",
                "files": sorted(files, key=lambda x: x["name"])[:50],
                "fileCount": len(files),
                "totalSize": total_size,
                "mtime": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
            })
        elif item.is_file():
            result.append({
                "name": item.name,
                "type": "file",
                "size": item.stat().st_size,
                "mtime": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
            })
    return result


def parse_output_line(line: str):
    """Parse a line from subprocess stdout into structured job state updates."""
    global job_state

    # File progress / status
    m = re.match(r'^\[FIL\]\s+(\w+)\s+(.+)$', line)
    if m:
        action, name = m.group(1), m.group(2).strip()
        if action == "START":
            # create or update file entry
            entry = None
            for f in job_state["files"]:
                if f["name"] == name:
                    entry = f
                    break
            if not entry:
                entry = {"name": name, "status": "downloading", "pct": 0, "size": "?"}
                job_state["files"].append(entry)
            else:
                entry["status"] = "downloading"
        elif action == "DONE":
            for f in job_state["files"]:
                if f["name"] == name:
                    f["status"] = "done"
                    break
        elif action == "FAIL":
            for f in job_state["files"]:
                if f["name"] == name:
                    f["status"] = "fail"
                    break
        elif action == "SKIP":
            job_state["files"].append({"name": name, "status": "skip", "pct": 0, "size": "?"})
        return

    # Download progress
    m = re.match(r'^\[DWN\]\s+(.+?)\s+([\d.]+)%\s+(\d+)/(\d+)', line)
    if m:
        name = m.group(1).strip()
        pct = float(m.group(2))
        downloaded = int(m.group(3))
        total = int(m.group(4))
        size_str = human_size(total)
        for f in job_state["files"]:
            if f["name"] == name:
                f["pct"] = round(pct, 1)
                f["size"] = size_str
                f["downloaded"] = human_size(downloaded)
                break
        return

    # URL / album start
    m = re.match(r'^\[URL\]\s+(.+)$', line)
    if m:
        job_state["urlLabel"] = m.group(1).strip()
        return

    # Summary
    m = re.match(r'^\[SUM\]\s+completed=(\d+)\s+failed=(\d+)\s+skipped=(\d+)\s+time=(.+)$', line)
    if m:
        job_state["summary"] = {
            "completed": int(m.group(1)),
            "failed": int(m.group(2)),
            "skipped": int(m.group(3)),
            "time": m.group(4),
        }
        return

    # Log line (from logging.info via --disable-ui)
    m = re.match(r'^\[(\d{2}:\d{2}:\d{2})\]\s+Event:\s+(.+?)\s+\|\s+Details:\s+(.+)$', line)
    if m:
        ts, event, details = m.group(1), m.group(2).strip(), m.group(3).strip()
        job_state["logs"].append({"ts": ts, "event": event, "details": details})
        if len(job_state["logs"]) > 200:
            job_state["logs"] = job_state["logs"][-200:]
        return

    # Fallback: raw line as log
    stripped = line.strip()
    if stripped and len(stripped) < 300:
        job_state["logs"].append({"ts": datetime.now().strftime("%H:%M:%S"), "event": "", "details": stripped})
        if len(job_state["logs"]) > 200:
            job_state["logs"] = job_state["logs"][-200:]


def human_size(n):
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024*1024):.1f} MB"
    return f"{n / (1024*1024*1024):.2f} GB"


def run_download_thread(urls, settings):
    global download_process, download_running, job_state

    with download_lock:
        if download_running:
            return
        download_running = True
        reset_job()
        job_state["phase"] = "starting"
        job_state["startTime"] = datetime.now().isoformat()
        job_state["urlCount"] = len(urls)

    try:
        urls_txt = "\n".join(u.strip() for u in urls if u.strip()) + "\n"
        URLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        URLS_FILE.write_text(urls_txt)

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        # Proxy
        http_proxy = settings.get("httpProxy", "").strip()
        https_proxy = settings.get("httpsProxy", "").strip()
        if http_proxy:
            env["HTTP_PROXY"] = http_proxy
            env["http_proxy"] = http_proxy
        if https_proxy:
            env["HTTPS_PROXY"] = https_proxy
            env["https_proxy"] = https_proxy
        if http_proxy or https_proxy:
            env["NO_PROXY"] = "localhost,127.0.0.1,::1"
            env["no_proxy"] = "localhost,127.0.0.1,::1"

        base_path = settings.get("downloadPath", "/data/downloads")
        subdir = settings.get("downloadSubdir", "").strip().strip("/")
        dl_path = str(Path(base_path) / subdir) if subdir else base_path
        Path(dl_path).mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable, "main.py",
            "--custom-path", dl_path,
            "--max-retries", str(settings.get("maxRetries", 5)),
            "--disable-ui",
        ]
        if settings.get("ignoreList", "").strip():
            cmd += ["--ignore"] + settings["ignoreList"].strip().split()
        if settings.get("includeList", "").strip():
            cmd += ["--include"] + settings["includeList"].strip().split()

        job_state["phase"] = "running"

        with subprocess.Popen(
            cmd,
            cwd="/app",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        ) as proc:
            download_process = proc
            for line in proc.stdout:
                stripped = line.rstrip()
                if stripped:
                    parse_output_line(stripped)
            proc.wait()

    except Exception as e:
        job_state["logs"].append({"ts": datetime.now().strftime("%H:%M:%S"), "event": "ERROR", "details": str(e)})
    finally:
        with download_lock:
            job_state["phase"] = "idle"
            job_state["endTime"] = datetime.now().isoformat()
            download_running = False
            download_process = None


# ── routes ──
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "running": download_running,
        "job": job_state,
    })


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json(force=True)
        s = load_settings()
        for k in ("concurrency", "maxRetries", "downloadPath", "downloadSubdir", "ignoreList", "includeList", "httpProxy", "httpsProxy"):
            if k in data:
                s[k] = data[k]
                if k in ("concurrency", "maxRetries"):
                    s[k] = int(s[k])
        save_settings(s)
        return jsonify({"ok": True, "settings": s})
    return jsonify(load_settings())


@app.route("/api/downloads")
def api_downloads():
    return jsonify(list_downloads())


@app.route("/api/logs")
def api_logs():
    return jsonify({"log": read_logs(200)})


@app.route("/api/subdirs", methods=["GET", "POST"])
def api_subdirs():
    dl = DOWNLOADS_DIR
    dl.mkdir(parents=True, exist_ok=True)
    if request.method == "POST":
        data = request.get_json(force=True)
        name = data.get("name", "").strip().strip("/")
        if name and "/" not in name:
            (dl / name).mkdir(parents=True, exist_ok=True)
        return jsonify({"ok": True})
    dirs = [
        {"name": d.name, "path": f"/data/downloads/{d.name}"}
        for d in sorted(dl.iterdir()) if d.is_dir()
    ]
    dirs.insert(0, {"name": "(根目录)", "path": "/data/downloads"})
    return jsonify(dirs)


@app.route("/api/start", methods=["POST"])
def api_start():
    global download_running
    with download_lock:
        if download_running:
            return jsonify({"ok": False, "error": "Download already running"})

    data = request.get_json(force=True)
    urls = data.get("urls", [])
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        return jsonify({"ok": False, "error": "No URLs provided"})

    settings = load_settings()
    if "settings" in data:
        for k, v in data["settings"].items():
            settings[k] = v

    t = threading.Thread(target=run_download_thread, args=(urls, settings), daemon=True)
    t.start()
    return jsonify({"ok": True, "urlCount": len(urls)})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global download_process, download_running
    with download_lock:
        if not download_running:
            return jsonify({"ok": False, "error": "No download running"})
        if download_process:
            download_process.terminate()
            job_state["logs"].append({"ts": datetime.now().strftime("%H:%M:%S"), "event": "STOPPED", "details": "Download stopped by user"})
    return jsonify({"ok": True})


@app.route("/api/urls", methods=["GET", "POST"])
def api_urls():
    if request.method == "POST":
        data = request.get_json(force=True)
        urls = data.get("urls", [])
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.splitlines() if u.strip()]
        URLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        URLS_FILE.write_text("\n".join(urls) + "\n")
        return jsonify({"ok": True, "count": len(urls)})

    if URLS_FILE.exists():
        urls = [u.strip() for u in URLS_FILE.read_text().splitlines() if u.strip()]
    else:
        urls = []
    return jsonify({"urls": urls})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8877, debug=False)
