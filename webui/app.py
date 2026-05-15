"""BunkrDownloader WebUI - Flask app wrapping the BunkrDownloader CLI."""
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from waitress import serve

app = Flask(__name__)

DATA_DIR = Path("/data")
CONFIG_DIR = DATA_DIR / "config"
DOWNLOADS_DIR = DATA_DIR / "downloads"
LOGS_DIR = DATA_DIR / "logs"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
URLS_FILE = CONFIG_DIR / "URLs.txt"
LOG_FILE = LOGS_DIR / "session.log"
HISTORY_FILE = CONFIG_DIR / "download_history.json"
MAX_HISTORY = 500

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

# ── runtime state ──
download_process = None
download_lock = threading.Lock()
download_running = False

job_state = {
    "phase": "idle",
    "urlCount": 0,
    "urlLabel": "",
    "downloadPath": "",
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
        "urlLabel": "",
        "downloadPath": "",
        "files": [],
        "logs": [],
        "summary": None,
        "startTime": None,
        "endTime": None,
    }


# ── settings ──
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


# ── history ──
def load_history():
    try:
        if HISTORY_FILE.exists():
            return json.loads(HISTORY_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        pass
    return []


def save_history(entries):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2))


def record_history(filename, url, status, size_bytes, dl_path):
    entries = load_history()
    entry = {
        "id": len(entries) + 1,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "filename": filename,
        "url": url,
        "status": status,
        "size": size_bytes,
        "path": dl_path,
    }
    entries.insert(0, entry)
    if len(entries) > MAX_HISTORY:
        entries = entries[:MAX_HISTORY]
    for i, e in enumerate(entries):
        e["id"] = i + 1
    save_history(entries)


# ── logs ──
def read_logs(tail=200):
    if not LOG_FILE.exists():
        return ""
    return "\n".join(LOG_FILE.read_text(errors="replace").splitlines()[-tail:])


def human_size(n):
    if n == 0:
        return "0 B"
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024*1024):.1f} MB"
    return f"{n / (1024*1024*1024):.2f} GB"


# ── subprocess output parser ──
def parse_output_line(line: str):
    global job_state

    # File status: [FIL] START|DONE|FAIL|SKIP <name>
    m = re.match(r'^\[FIL\]\s+(\w+)\s+(.+)$', line)
    if m:
        action, name = m.group(1), m.group(2).strip()
        if action == "START":
            found = False
            for f in job_state["files"]:
                if f["name"] == name:
                    f["status"] = "downloading"
                    found = True
                    break
            if not found:
                job_state["files"].append({"name": name, "status": "downloading", "pct": 0, "size": "?"})
        elif action == "DONE":
            total_bytes = 0
            for f in job_state["files"]:
                if f["name"] == name:
                    f["status"] = "done"
                    total_bytes = f.get("totalBytes", 0)
                    break
            record_history(name, job_state.get("urlLabel", ""), "完成",
                           total_bytes, job_state.get("downloadPath", ""))
        elif action == "FAIL":
            total_bytes = 0
            for f in job_state["files"]:
                if f["name"] == name:
                    f["status"] = "fail"
                    total_bytes = f.get("totalBytes", 0)
                    break
            record_history(name, job_state.get("urlLabel", ""), "失败",
                           total_bytes, job_state.get("downloadPath", ""))
        elif action == "SKIP":
            reason = name
            job_state["files"].append({"name": reason, "status": "skip", "pct": 0, "size": "?"})
            record_history(reason, job_state.get("urlLabel", ""), "跳过", 0,
                           job_state.get("downloadPath", ""))
        return

    # Progress: [DWN] <filename> <pct>% <downloaded>/<total>
    m = re.match(r'^\[DWN\]\s+(.+?)\s+([\d.]+)%\s+(\d+)/(\d+)', line)
    if m:
        name = m.group(1).strip()
        pct = float(m.group(2))
        downloaded = int(m.group(3))
        total = int(m.group(4))
        for f in job_state["files"]:
            if f["name"] == name:
                f["pct"] = round(pct, 1)
                f["size"] = human_size(total)
                f["downloaded"] = human_size(downloaded)
                f["totalBytes"] = total
                break
        return

    # URL context: [URL] <url>
    m = re.match(r'^\[URL\]\s+(.+)$', line)
    if m:
        job_state["urlLabel"] = m.group(1).strip()
        return

    # Summary: [SUM] completed=N failed=N skipped=N time=...
    m = re.match(r'^\[SUM\]\s+completed=(\d+)\s+failed=(\d+)\s+skipped=(\d+)\s+time=(.+)$', line)
    if m:
        job_state["summary"] = {
            "completed": int(m.group(1)),
            "failed": int(m.group(2)),
            "skipped": int(m.group(3)),
            "time": m.group(4),
        }
        return

    # Log: [HH:MM:SS] Event: ... | Details: ...
    m = re.match(r'^\[(\d{2}:\d{2}:\d{2})\]\s+Event:\s+(.+?)\s+\|\s+Details:\s+(.+)$', line)
    if m:
        ts, event, details = m.group(1), m.group(2).strip(), m.group(3).strip()
        job_state["logs"].append({"ts": ts, "event": event, "details": details})
        if len(job_state["logs"]) > 200:
            job_state["logs"] = job_state["logs"][-200:]
        return

    # Fallback
    stripped = line.strip()
    if stripped and len(stripped) < 300:
        job_state["logs"].append({"ts": datetime.now().strftime("%H:%M:%S"), "event": "", "details": stripped})
        if len(job_state["logs"]) > 200:
            job_state["logs"] = job_state["logs"][-200:]


# ── download runner ──
def run_download_thread(urls, settings):
    global download_process, download_running

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
        # BunkrDownloader reads URLs.txt from its working directory (/app)
        Path("/app/URLs.txt").write_text(urls_txt)

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["TERM"] = "dumb"

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
        job_state["downloadPath"] = dl_path

        with subprocess.Popen(
            cmd, cwd="/app",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env,
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
    return jsonify({"running": download_running, "job": job_state})


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


@app.route("/api/history")
def api_history():
    return jsonify(load_history())


@app.route("/api/logs")
def api_logs():
    return jsonify({"log": read_logs(200)})


@app.route("/api/subdirs", methods=["GET", "POST"])
def api_subdirs():
    dl = DOWNLOADS_DIR
    dl.mkdir(parents=True, exist_ok=True)
    if request.method == "POST":
        data = request.get_json(force=True)
        rel = data.get("name", "").strip().strip("/")
        if rel and "/" not in rel and ".." not in rel:
            (dl / rel).mkdir(parents=True, exist_ok=True)
        return jsonify({"ok": True})
    dirs = [{"name": "(根目录)", "path": "/data/downloads"}]
    _walk_subdirs(dl, "", dirs)
    if len(dirs) > 1:
        dirs[1:] = sorted(dirs[1:], key=lambda d: d["name"])
    return jsonify(dirs)


def _walk_subdirs(base: Path, prefix: str, out: list):
    try:
        for child in sorted(base.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                rel = f"{prefix}/{child.name}" if prefix else child.name
                out.append({"name": rel, "path": str(child)})
                _walk_subdirs(child, rel, out)
    except PermissionError:
        pass


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
        return jsonify({"urls": [u.strip() for u in URLS_FILE.read_text().splitlines() if u.strip()]})
    return jsonify({"urls": []})


if __name__ == "__main__":
    serve(app, host="0.0.0.0", port=8877)
