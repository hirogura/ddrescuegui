#!/usr/bin/env python3
import http.server
import json
import os
import subprocess
import time
import signal
import threading
import urllib.request
from urllib.parse import urlparse, parse_qs

PORT = 3327
VERSION = "1.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/hirogura/ddrescuegui/main/install.sh"
SERVICE_NAME = "ddrescuegui"

running_process = None
current_log_file = None

def get_block_devices():
    devices = []
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MODEL,SERIAL,MOUNTPOINT,TRAN,FSTYPE"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            for dev in data.get("blockdevices", []):
                if dev.get("type") == "disk":
                    name = dev.get("name", "")
                    size = dev.get("size", "")
                    model = (dev.get("model") or "").strip()
                    serial = (dev.get("serial") or "").strip()
                    tran = (dev.get("tran") or "").strip()
                    mount = dev.get("mountpoint") or ""
                    fstype = (dev.get("fstype") or "").strip()
                    label = f"/dev/{name} - {size}"
                    if model: label += f" ({model})"
                    if serial: label += f" [{serial}]"
                    if tran: label += f" ({tran})"
                    if fstype: label += f" [{fstype}]"
                    if mount: label += f" mounted:{mount}"
                    devices.append({"path": f"/dev/{name}", "name": name, "size": size,
                        "model": model, "serial": serial, "tran": tran,
                        "mountpoint": mount, "fstype": fstype, "label": label})
    except Exception:
        pass
    return devices

def get_device_info(path):
    info = {}
    try:
        r = subprocess.run(["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL", path],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            info["lsblk"] = r.stdout.strip()
    except Exception:
        pass
    try:
        r = subprocess.run(["blkid", path], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            info["blkid"] = r.stdout.strip()
    except Exception:
        pass
    try:
        r = subprocess.run(["file", "-s", path], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            info["file_type"] = r.stdout.strip()
    except Exception:
        pass
    return info

def get_file_info(path, for_dest=False):
    info = {}
    if not os.path.exists(path):
        if for_dest:
            info["status"] = "新規作成"
            dir_path = os.path.dirname(path)
            if dir_path and os.path.isdir(dir_path):
                info["target_dir"] = f"ディレクトリ: {dir_path}"
                try:
                    st = os.statvfs(dir_path)
                    free = st.f_bavail * st.f_frsize
                    info["free_space"] = f"空き容量: {free / (1024**3):.2f} GB"
                except Exception:
                    pass
            else:
                info["target_dir"] = f"ディレクトリが見つかりません: {dir_path}"
        else:
            info["error"] = "ファイルが見つかりません"
        return info
    try:
        stat = os.stat(path)
        info["size"] = stat.st_size
        info["size_human"] = f"{stat.st_size / (1024**3):.2f} GB"
        info["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
    except Exception:
        pass
    try:
        r = subprocess.run(["file", path], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            info["file_type"] = r.stdout.strip()
    except Exception:
        pass
    return info

def get_log_files():
    logs = []
    if os.path.isdir(LOG_DIR):
        for f in sorted(os.listdir(LOG_DIR), reverse=True):
            if f.endswith(".log"):
                fpath = os.path.join(LOG_DIR, f)
                logs.append({"name": f, "size": os.path.getsize(fpath), "mtime": os.path.getmtime(fpath)})
    return logs

def read_log_tail(filepath, lines=100):
    try:
        with open(filepath, "r", errors="replace") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
    except Exception:
        return ""

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(BASE_DIR, "public"), **kw)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/api/devices":
            self._json(get_block_devices())
        elif p.path == "/api/device-info":
            q = parse_qs(p.query)
            path = q.get("path", [""])[0]
            self._json(get_device_info(path) if path else {"error": "path required"}, 400 if not path else 200)
        elif p.path == "/api/file-info":
            q = parse_qs(p.query)
            path = q.get("path", [""])[0]
            for_dest = q.get("for_dest", ["0"])[0] == "1"
            self._json(get_file_info(path, for_dest) if path else {"error": "path required"}, 400 if not path else 200)
        elif p.path == "/api/status":
            self._json({"running": running_process is not None and running_process.poll() is None,
                        "log_file": current_log_file, "version": VERSION})
        elif p.path == "/api/logs":
            self._json(get_log_files())
        elif p.path == "/api/log-content":
            q = parse_qs(p.query)
            name = q.get("name", [""])[0]
            lines = int(q.get("lines", ["100"])[0])
            if name:
                fpath = os.path.join(LOG_DIR, name)
                self._json({"content": read_log_tail(fpath, lines)} if os.path.exists(fpath) else {"error": "not found"}, 404 if not os.path.exists(fpath) else 200)
            else:
                self._json({"error": "name required"}, 400)
        elif p.path == "/api/log-stream":
            q = parse_qs(p.query)
            name = q.get("name", [""])[0]
            if name: self._stream_log(os.path.join(LOG_DIR, name))
            else: self._json({"error": "name required"}, 400)
        else:
            super().do_GET()

    def do_POST(self):
        p = urlparse(self.path)
        cl = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(cl) if cl else b""
        try: data = json.loads(body) if body else {}
        except: data = {}
        if p.path == "/api/start": self._handle_start(data)
        elif p.path == "/api/stop": self._handle_stop()
        elif p.path == "/api/force-stop": self._handle_force_stop()
        elif p.path == "/api/update": self._handle_update()
        elif p.path == "/api/restart": self._handle_restart()
        else: self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _handle_start(self, data):
        global running_process, current_log_file
        if running_process and running_process.poll() is None:
            self._json({"error": "既に実行中です"}); return

        source = data.get("source", "")
        dest = data.get("dest", "")
        options = data.get("options", {})
        resume_log = data.get("resume_log", "")

        if not source: self._json({"error": "コピー元を指定してください"}); return
        if not dest: self._json({"error": "コピー先を指定してください"}); return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_name = resume_log if resume_log else f"ddrescue_{os.path.basename(source).replace('/', '_')[:20]}_{timestamp}.run.log"
        current_log_file = os.path.join(LOG_DIR, log_name)
        mapfile_path = current_log_file[:-len(".run.log")] + ".map" if current_log_file.endswith(".run.log") else current_log_file + ".map"

        cmd = ["stdbuf", "-o0", "-e0", "ddrescue"]
        if options.get("direct"): cmd.append("-d")
        if options.get("force"): cmd.append("-f")
        if options.get("no_scrape"): cmd.append("-n")
        if options.get("no_sweep"): cmd.append("-N")
        if options.get("sparse"): cmd.append("-S")
        if options.get("odirect"): cmd.append("-D")
        if options.get("reverse"): cmd.append("-R")
        if options.get("unidirectional"): cmd.append("-u")

        for opt, flag in [
            ("retry_passes", "-r"), ("input_pos", "-i"),
            ("size_limit", "-s"), ("sector_size", "-b"), ("cluster_size", "-c"),
            ("min_read_rate", "-a"), ("max_bad_areas", "-e"),
            ("max_error_rate", "-E"), ("timeout", "-T"),
        ]:
            val = options.get(opt, "")
            if val and str(val).strip():
                cmd.extend([flag, str(val).strip()])

        cmd.extend([source, dest, mapfile_path])

        try:
            log_f = open(current_log_file, "a")
            log_f.write(f"=== ddrescue started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            log_f.write(f"Command: {' '.join(cmd)}\n\n")
            log_f.flush()
            running_process = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
            log_f.close()
            self._json({"ok": True, "log_file": log_name, "pid": running_process.pid})
        except Exception as e:
            self._json({"error": str(e)})

    def _handle_stop(self):
        global running_process
        if running_process and running_process.poll() is None:
            proc = running_process
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception: pass

            def escalate():
                try:
                    if proc.poll() is None:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception: pass
            threading.Timer(3.0, escalate).start()
            self._json({"ok": True})
        else:
            self._json({"error": "実行中のプロセスがありません"})

    def _handle_force_stop(self):
        global running_process
        if running_process and running_process.poll() is None:
            try:
                os.killpg(os.getpgid(running_process.pid), signal.SIGKILL)
            except Exception: pass
            self._json({"ok": True})
        else:
            self._json({"error": "実行中のプロセスがありません"})

    def _handle_update(self):
        if running_process and running_process.poll() is None:
            self._json({"error": "レスキュー実行中はアップデートできません"}); return
        script_path = "/tmp/ddrescuegui-install.sh"
        try:
            urllib.request.urlretrieve(INSTALL_SCRIPT_URL, script_path)
            os.chmod(script_path, 0o755)
        except Exception as e:
            self._json({"error": f"インストーラのダウンロードに失敗しました: {e}"}); return
        try:
            log_f = open(os.path.join(LOG_DIR, "update.log"), "w")
            subprocess.Popen(["bash", script_path], stdout=log_f,
                stderr=subprocess.STDOUT, start_new_session=True)
            log_f.close()
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": str(e)})

    def _handle_restart(self):
        if running_process and running_process.poll() is None:
            self._json({"error": "レスキュー実行中は再起動できません"}); return

        def do_restart():
            try:
                subprocess.run(["systemctl", "restart", SERVICE_NAME], timeout=30)
            except Exception: pass
        threading.Timer(0.5, do_restart).start()
        self._json({"ok": True})

    def _sse_send(self, text):
        escaped = text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
        self.wfile.write(f"data: {escaped}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _stream_log(self, filepath):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        interval = 0.5
        try:
            with open(filepath, "r", errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                start = max(0, size - 4096)
                f.seek(start)
                pending = ""
                last_send = 0.0
                initial = f.read()
                if start > 0:
                    nl = initial.find("\n")
                    initial = initial[nl + 1:] if nl != -1 else ""
                if initial:
                    self._sse_send(initial)
                    last_send = time.time()
                while True:
                    chunk = f.read(8192)
                    if chunk:
                        pending += chunk
                    alive = running_process is not None and running_process.poll() is None
                    now = time.time()
                    if pending and (not alive or now - last_send >= interval):
                        self._sse_send(pending)
                        pending = ""
                        last_send = now
                    if not alive:
                        break
                    time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError): pass

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *a): pass

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"ddrescueGUI running on port {PORT}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        if running_process and running_process.poll() is None:
            os.killpg(os.getpgid(running_process.pid), signal.SIGTERM)
        server.server_close()
