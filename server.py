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
VERSION = "1.4.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/hirogura/ddrescuegui/main/install.sh"
SERVICE_NAME = "ddrescuegui"

running_process = None
current_log_file = None

# ---- ディスク完全消去ジョブ管理 ----
import re
wipe_job = None
wipe_lock = threading.Lock()
# 消去対象として許可するデバイス名（ホールディスクのみ。パーティションは不可）
WIPE_PATH_RE = re.compile(r"^/dev/(sd[a-z]+|hd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")
WIPE_CHUNK = 4 * 1024 * 1024  # 4MB ずつ書き込む

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

# ---- ディスク完全消去用ヘルパー ----

def get_wipe_devices():
    """消去ページ用：パーティション情報・SSDヒント付きデバイス一覧"""
    devices = []
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MODEL,SERIAL,TRAN,FSTYPE,MOUNTPOINT"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            return get_block_devices()
        data = json.loads(r.stdout)
        for dev in data.get("blockdevices", []):
            if dev.get("type") != "disk":
                continue
            name = dev.get("name", "")
            # 仮想デバイス（zram/loop/dm/md等）は対象外。実ディスクのみ表示
            if not WIPE_PATH_RE.match(f"/dev/{name}"):
                continue
            size_bytes = int(dev.get("size", 0) or 0)
            model = (dev.get("model") or "").strip()
            serial = (dev.get("serial") or "").strip()
            tran = (dev.get("tran") or "").strip()
            # 人間可読サイズ
            if size_bytes >= 1024**3:
                size = f"{size_bytes / (1024**3):.1f} GB"
            elif size_bytes >= 1024**2:
                size = f"{size_bytes / (1024**2):.1f} MB"
            else:
                size = f"{size_bytes} B"
            # 回転ディスク判定（0=SSD/NVMe、1=HDD、不明はNone）
            rotational = None
            ssd_hint = False
            try:
                with open(f"/sys/block/{name}/queue/rotational", "r") as f:
                    rotational = int(f.read().strip())
                    ssd_hint = (rotational == 0)
            except Exception:
                pass
            # パーティション一覧
            partitions = []
            has_mount = bool(dev.get("mountpoint"))
            for child in dev.get("children") or []:
                cname = child.get("name", "")
                csize = int(child.get("size", 0) or 0)
                if csize >= 1024**3:
                    csize_h = f"{csize / (1024**3):.1f} GB"
                elif csize >= 1024**2:
                    csize_h = f"{csize / (1024**2):.1f} MB"
                else:
                    csize_h = f"{csize} B"
                cmount = child.get("mountpoint") or ""
                if cmount:
                    has_mount = True
                partitions.append({
                    "name": cname, "path": f"/dev/{cname}",
                    "size": csize_h, "size_bytes": csize,
                    "fstype": (child.get("fstype") or "").strip(),
                    "mountpoint": cmount,
                })
            label = f"/dev/{name} - {size}"
            if model: label += f" ({model})"
            if serial: label += f" [{serial}]"
            if tran: label += f" ({tran})"
            devices.append({"path": f"/dev/{name}", "name": name, "size": size,
                "size_bytes": size_bytes, "model": model, "serial": serial,
                "tran": tran, "label": label, "partitions": partitions,
                "has_mount": has_mount, "rotational": rotational,
                "ssd_hint": ssd_hint})
    except Exception:
        pass
    return devices


def get_device_size_bytes(path):
    """blockdev でデバイスのバイト数を取得"""
    try:
        r = subprocess.run(["blockdev", "--getsize64", path],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return int(r.stdout.strip())
    except Exception:
        pass
    return 0


# ---- S.M.A.R.T.情報取得用ヘルパー ----

def get_smart_devices():
    """S.M.A.R.T.ページ用：接続ディスク一覧（消去ページと同等の実ディスク一覧）"""
    devs = get_wipe_devices()
    if devs:
        return devs
    # フォールバック：lsblk の disk 一覧から実デバイスのみ返す
    out = []
    for d in get_block_devices():
        if WIPE_PATH_RE.match(d.get("path", "")):
            d.setdefault("partitions", [])
            d.setdefault("has_mount", bool(d.get("mountpoint")))
            d.setdefault("ssd_hint", False)
            out.append(d)
    return out


def get_smart_info(path):
    """指定ディスクの S.M.A.R.T.情報を取得。smartctl の JSON + テキストを返す"""
    if not WIPE_PATH_RE.match(path or ""):
        return {"path": path, "available": False,
                "error": f"不正なデバイス指定です: {path}"}
    if not os.path.exists(path):
        return {"path": path, "available": False,
                "error": f"デバイスが見つかりません: {path}"}
    # smartctl 本体の存在確認
    try:
        r_ver = subprocess.run(["smartctl", "--version"],
            capture_output=True, text=True, timeout=5)
        if r_ver.returncode != 0 and not (r_ver.stdout or ""):
            return {"path": path, "available": False,
                    "error": "smartctl が利用できません（smartmontools を導入してください）"}
    except FileNotFoundError:
        return {"path": path, "available": False,
                "error": "smartctl が見つかりません（smartmontools を導入してください）"}
    except Exception as e:
        return {"path": path, "available": False, "error": f"smartctl 確認エラー: {e}"}

    # JSON 形式で全情報を取得（終了コードはビットマスクのため成否判定に使わない）
    smart_json = None
    try:
        r = subprocess.run(["smartctl", "-a", "-j", path],
            capture_output=True, text=True, timeout=20)
        raw = (r.stdout or "").strip()
        if raw:
            try:
                smart_json = json.loads(raw)
            except Exception:
                smart_json = None
    except subprocess.TimeoutExpired:
        return {"path": path, "available": False, "error": "smartctl がタイムアウトしました"}
    except Exception as e:
        return {"path": path, "available": False, "error": f"smartctl 実行エラー: {e}"}

    # テキスト形式も併せて取得（画面の「詳細」表示用）
    text_out = ""
    try:
        r2 = subprocess.run(["smartctl", "-a", path],
            capture_output=True, text=True, timeout=20)
        text_out = (r2.stdout or "") + (r2.stderr or "")
        text_out = text_out.strip()
    except Exception:
        pass

    if smart_json is None and not text_out:
        return {"path": path, "available": False,
                "error": "S.M.A.R.T.情報を取得できませんでした"}

    # 利用可否・ヘルス判定
    available = True
    health = "不明"
    health_ok = None
    support_msg = ""
    if smart_json is not None:
        try:
            status = smart_json.get("smart_status") or {}
            if "passed" in status:
                health_ok = bool(status.get("passed"))
                health = "正常" if health_ok else "異常あり"
            # NVMe でも smart_status.passed が入る。無い場合は全体ステータスで補完
            if health_ok is None:
                # exit_status 等から推測できないため不明のまま
                pass
            sup = smart_json.get("smart_support") or {}
            # smart_support.available が false の場合は S.M.A.R.T. 非対応
            if sup.get("available") is False:
                available = False
                support_msg = "このディスクは S.M.A.R.T. に対応していません"
            # デバイス open エラー時は利用不可
            msgs = smart_json.get("messages") or []
            for m in msgs:
                s = (m.get("string") or "") if isinstance(m, dict) else str(m)
                if "unable to" in s.lower() or "failed" in s.lower() or "error" in s.lower():
                    pass
        except Exception:
            pass
    # JSON が取れずテキストのみの場合、テキストから簡易判定
    if smart_json is None and text_out:
        low = text_out.lower()
        if "smart support is: unavailable" in low or "device does not support smart" in low:
            available = False
            support_msg = "このディスクは S.M.A.R.T. に対応していません"
        elif "smart overall-health self-assessment test result: passed" in low:
            health, health_ok = "正常", True
        elif "smart overall-health self-assessment test result: failed" in low:
            health, health_ok = "異常あり", False

    if not available:
        return {"path": path, "available": False,
                "health": health, "health_ok": health_ok,
                "error": support_msg or "S.M.A.R.T. に対応していません",
                "output": text_out, "data": smart_json}

    return {"path": path, "available": True,
            "health": health, "health_ok": health_ok,
            "output": text_out, "data": smart_json}


def _wipe_log(job, msg):
    """消去ジョブのログファイルに追記"""
    try:
        lf = job.get("log_file")
        if lf:
            with open(lf, "a") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _wipe_write_pass(job, target, fill, pass_no, pass_total, log_prefix):
    """1パス分の上書き。fill='zero' または 'random'。停止要求時は False を返す"""
    size = target["size_bytes"]
    chunk = WIPE_CHUNK
    zero_block = b"\x00" * chunk
    written = target.get("pass_written_base", 0)
    t0 = time.time()
    try:
        fd = os.open(target["path"], os.O_WRONLY)
    except Exception as e:
        target["status"] = "error"
        target["message"] = f"オープン失敗: {e}"
        return False
    try:
        # 先頭にシーク（O_WRONLY では offset 0 から開始されるが明示）
        os.lseek(fd, 0, os.SEEK_SET)
        # 既に書き込み済みバイトがある場合（再開ではないが念のため）スキップ
        remaining = size
        # パス開始時点の基準値を記録
        base = 0
        while remaining > 0:
            if job.get("stop"):
                target["status"] = "stopped"
                target["message"] = "ユーザーにより中断"
                return False
            n = chunk if remaining >= chunk else remaining
            if fill == "zero":
                buf = zero_block[:n] if n != chunk else zero_block
            else:
                buf = os.urandom(n)
            try:
                w = os.write(fd, buf)
            except Exception as e:
                target["status"] = "error"
                target["message"] = f"書き込み失敗: {e}"
                return False
            if w == 0:
                target["status"] = "error"
                target["message"] = "書き込みが 0 バイトで終了"
                return False
            base += w
            remaining -= w
            # 進捗更新（このパスの進捗＋全体パス換算）
            target["bytes_written"] = target.get("bytes_written", 0) + 0  # 全体は下で再計算
            elapsed = time.time() - t0
            # このパス内の割合
            pass_frac = base / size if size else 1.0
            # 全体割合 = (完了パス + 当パス進捗) / 全パス
            overall = ((pass_no - 1) + pass_frac) / pass_total if pass_total else 1.0
            target["percent"] = round(overall * 100, 1)
            target["pass_current"] = pass_no
            target["pass_total"] = pass_total
            if elapsed > 0 and base > 0:
                speed = base / elapsed
                target["speed_bps"] = speed
                left_in_pass = size - base
                passes_left = (pass_total - pass_no) * size + left_in_pass
                target["eta_sec"] = int(passes_left / speed) if speed > 0 else -1
            target["message"] = f"{log_prefix} パス {pass_no}/{pass_total} 書き込み中"
        os.fsync(fd)
        target["bytes_written"] = (pass_no * size)
        return True
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def _wipe_ssd(job, target, tran):
    """SSD 消去。接続方式でコマンドを自動選択。進捗は取れないため開始/完了のみ"""
    path = target["path"]
    tran_low = (tran or "").lower()
    if tran_low == "nvme":
        target["method_detail"] = "NVMe Format (Sanitize相当: nvme format --ses=1)"
        cmd = ["nvme", "format", path, "--ses=1", "--force"]
    elif tran_low in ("sata", "ata", "sas"):
        target["method_detail"] = "Secure Erase (hdparm)"
        cmd = None  # hdparm は複数ステップのため後述
    else:
        # USB 接続等：Secure Erase が使えないため blkdiscard（TRIM/Sanitize相当）
        target["method_detail"] = "blkdiscard による破棄（USB経由等のためSanitize相当）"
        cmd = ["blkdiscard", "-f", path]
    _wipe_log(job, f"{path}: {target['method_detail']} 開始")
    try:
        if cmd is not None:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            out = (r.stdout or "") + (r.stderr or "")
            _wipe_log(job, f"{path}: 終了 code={r.returncode}\n{out}")
            if r.returncode == 0:
                target["percent"] = 100.0
                target["status"] = "done"
                target["message"] = "消去完了"
                return True
            # blkdiscard が未対応の場合はゼロ1パスにフォールバック
            if "blkdiscard" in target["method_detail"]:
                _wipe_log(job, f"{path}: blkdiscard失敗、ゼロ1パスにフォールバック")
                target["method_detail"] += " → blkdiscard非対応のためゼロ消去に切替"
                target["pass_total"] = 1
                ok = _wipe_write_pass(job, target, "zero", 1, 1, "SSDフォールバック(ゼロ)")
                if ok:
                    target["percent"] = 100.0
                    target["status"] = "done"
                    target["message"] = "消去完了（ゼロ1パス）"
                    return True
                return False
            target["status"] = "error"
            target["message"] = f"消去コマンド失敗 (code={r.returncode}): {out[:500]}"
            return False
        else:
            # hdparm Secure Erase（2ステップ）
            # Frozen チェック
            r = subprocess.run(["hdparm", "-I", path],
                capture_output=True, text=True, timeout=30)
            out = (r.stdout or "") + (r.stderr or "")
            if "frozen" in out.lower():
                # frozen と not frozen の両方を含む場合があるため "not frozen" を優先判定
                if "not frozen" not in out.lower():
                    target["status"] = "error"
                    target["message"] = "Frozen状態のためSecure Eraseできません（電源再投入で解除される場合があります）"
                    _wipe_log(job, f"{path}: frozen のため中止")
                    return False
            passwd = "ddrescuegui"
            r1 = subprocess.run(
                ["hdparm", "--user-master", "u", "--security-set-pass", passwd, path],
                capture_output=True, text=True, timeout=120)
            _wipe_log(job, f"{path}: security-set-pass code={r1.returncode} {(r1.stdout or '') + (r1.stderr or '')}")
            if r1.returncode != 0:
                target["status"] = "error"
                target["message"] = f"セキュリティパスワード設定失敗: {(r1.stderr or r1.stdout or '')[:300]}"
                return False
            if job.get("stop"):
                target["status"] = "stopped"
                return False
            r2 = subprocess.run(
                ["hdparm", "--user-master", "u", "--security-erase", passwd, path],
                capture_output=True, text=True, timeout=3600)
            _wipe_log(job, f"{path}: security-erase code={r2.returncode} {(r2.stdout or '') + (r2.stderr or '')}")
            if r2.returncode == 0:
                target["percent"] = 100.0
                target["status"] = "done"
                target["message"] = "消去完了（Secure Erase）"
                return True
            target["status"] = "error"
            target["message"] = f"Secure Erase失敗: {(r2.stderr or r2.stdout or '')[:300]}"
            return False
    except FileNotFoundError as e:
        target["status"] = "error"
        target["message"] = f"消去コマンドが見つかりません: {e}（nvme-cli / hdparm / util-linux を導入してください）"
        return False
    except subprocess.TimeoutExpired:
        target["status"] = "error"
        target["message"] = "消去コマンドがタイムアウト"
        return False
    except Exception as e:
        target["status"] = "error"
        target["message"] = f"消去エラー: {e}"
        return False


def _run_wipe_job(job):
    """消去ジョブのバックグラウンド実行（対象ディスクを順次処理）"""
    global wipe_job
    _wipe_log(job, f"消去ジョブ開始 method={job['method']} passes={job['passes']}")
    for idx, target in enumerate(job["targets"]):
        if job.get("stop"):
            if target["status"] == "waiting":
                target["status"] = "stopped"
                target["message"] = "中断"
            continue
        job["current_index"] = idx
        target["status"] = "running"
        target["percent"] = 0.0
        target["started_at"] = time.time()
        _wipe_log(job, f"{target['path']} 開始 (SSD={target['is_ssd']})")
        if target["is_ssd"]:
            ok = _wipe_ssd(job, target, target.get("tran", ""))
        else:
            if job["method"] == "random":
                passes = job["passes"]
                target["method_detail"] = f"乱数 {passes} 回上書き"
                ok = True
                for p in range(1, passes + 1):
                    if job.get("stop"):
                        target["status"] = "stopped"
                        target["message"] = "ユーザーにより中断"
                        ok = False
                        break
                    target["message"] = f"乱数 パス {p}/{passes} 書き込み中"
                    if not _wipe_write_pass(job, target, "random", p, passes, "乱数"):
                        ok = False
                        break
                if ok:
                    target["percent"] = 100.0
                    target["status"] = "done"
                    target["message"] = f"消去完了（乱数{passes}回）"
            else:
                target["method_detail"] = "ゼロ 1 回上書き"
                if _wipe_write_pass(job, target, "zero", 1, 1, "ゼロ"):
                    target["percent"] = 100.0
                    target["status"] = "done"
                    target["message"] = "消去完了（ゼロ1回）"
                    ok = True
                else:
                    ok = False
        target["finished_at"] = time.time()
        _wipe_log(job, f"{target['path']} 終了 status={target['status']} {target.get('message','')}")
    job["finished_at"] = time.time()
    job["running"] = False
    _wipe_log(job, "消去ジョブ終了")

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
            with wipe_lock:
                wrun = wipe_job is not None and bool(wipe_job.get("running"))
            self._json({"running": running_process is not None and running_process.poll() is None,
                        "log_file": current_log_file, "version": VERSION,
                        "wipe_running": wrun})
        elif p.path == "/api/logs":
            self._json(get_log_files())
        elif p.path == "/api/log-content":
            q = parse_qs(p.query)
            name = os.path.basename(q.get("name", [""])[0])
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
        elif p.path == "/api/wipe-devices":
            self._json(get_wipe_devices())
        elif p.path == "/api/smart-devices":
            self._json(get_smart_devices())
        elif p.path == "/api/smart":
            q = parse_qs(p.query)
            path = q.get("path", [""])[0]
            if not path:
                self._json({"available": False, "error": "path required"}, 400)
            else:
                self._json(get_smart_info(path))
        elif p.path == "/api/wipe/status":
            with wipe_lock:
                if wipe_job is None:
                    self._json({"running": False, "job": None})
                else:
                    # 進捗スナップショットを返す
                    self._json({"running": bool(wipe_job.get("running")),
                        "job": wipe_job})
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
        elif p.path == "/api/wipe/start": self._handle_wipe_start(data)
        elif p.path == "/api/wipe/stop": self._handle_wipe_stop()
        else: self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _wipe_running(self):
        with wipe_lock:
            return wipe_job is not None and bool(wipe_job.get("running"))

    def _handle_start(self, data):
        global running_process, current_log_file
        if running_process and running_process.poll() is None:
            self._json({"error": "既に実行中です"}); return
        if self._wipe_running():
            self._json({"error": "ディスク消去実行中はレスキューできません"}); return

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
        if self._wipe_running():
            self._json({"error": "ディスク消去実行中はアップデートできません"}); return
        script_path = "/tmp/ddrescuegui-install.sh"
        try:
            urllib.request.urlretrieve(INSTALL_SCRIPT_URL, script_path)
            os.chmod(script_path, 0o755)
        except Exception as e:
            self._json({"error": f"インストーラのダウンロードに失敗しました: {e}"}); return
        try:
            log_path = os.path.join(LOG_DIR, "update.log")
            with open(log_path, "w") as log_f: log_f.close()
            # systemd 管理下で別ユニット起動（サービス再起動時に道連れkillされるのを防ぐ）
            if os.path.exists("/usr/bin/systemd-run"):
                cmd = ["systemd-run", "--collect", "--unit=ddrescuegui-update",
                       "--description=ddrescueGUI update",
                       "bash", "-c", f"exec bash '{script_path}' > '{log_path}' 2>&1"]
                out = subprocess.DEVNULL
            else:
                cmd = ["bash", script_path]
                out = open(log_path, "w")
            subprocess.Popen(cmd, stdout=out,
                stderr=subprocess.STDOUT, start_new_session=True)
            if out is not subprocess.DEVNULL: out.close()
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": str(e)})

    def _handle_restart(self):
        if running_process and running_process.poll() is None:
            self._json({"error": "レスキュー実行中は再起動できません"}); return
        if self._wipe_running():
            self._json({"error": "ディスク消去実行中は再起動できません"}); return

        def do_restart():
            try:
                subprocess.run(["systemctl", "restart", SERVICE_NAME], timeout=30)
            except Exception: pass
        threading.Timer(0.5, do_restart).start()
        self._json({"ok": True})

    def _handle_wipe_start(self, data):
        global wipe_job
        if running_process and running_process.poll() is None:
            self._json({"error": "レスキュー実行中は消去できません"}); return
        with wipe_lock:
            if wipe_job is not None and bool(wipe_job.get("running")):
                self._json({"error": "既に消去実行中です"}); return
        targets_in = data.get("targets", [])
        method = data.get("method", "zero")
        try:
            passes = int(data.get("passes", 3))
        except Exception:
            passes = 3
        if method not in ("zero", "random"):
            self._json({"error": "消去方式が不正です"}); return
        passes = max(1, min(7, passes))
        if not targets_in:
            self._json({"error": "対象ディスクを選択してください"}); return
        # 最新デバイス一覧で検証（存在確認・マウント確認・サイズ取得）
        devs = {d["path"]: d for d in get_wipe_devices()}
        targets = []
        for t in targets_in:
            path = (t.get("path") or "").strip()
            is_ssd = bool(t.get("is_ssd"))
            if not WIPE_PATH_RE.match(path):
                self._json({"error": f"不正なデバイス指定です: {path}"}); return
            if path not in devs:
                self._json({"error": f"デバイスが見つかりません: {path}"}); return
            info = devs[path]
            if info.get("has_mount"):
                self._json({"error": f"{path} はマウント中のパーティションを含むため消去できません。アンマウントしてから実行してください"}); return
            size_bytes = info.get("size_bytes") or get_device_size_bytes(path)
            if not size_bytes or size_bytes <= 0:
                self._json({"error": f"{path} のサイズを取得できません"}); return
            # 二重指定を除去
            if any(x["path"] == path for x in targets):
                continue
            targets.append({"path": path, "name": info.get("name", ""),
                "size": info.get("size", ""), "size_bytes": size_bytes,
                "model": info.get("model", ""), "serial": info.get("serial", ""),
                "tran": info.get("tran", ""), "is_ssd": is_ssd,
                "status": "waiting", "percent": 0.0,
                "pass_current": 0, "pass_total": 1,
                "bytes_written": 0, "speed_bps": 0, "eta_sec": -1,
                "message": "待機中", "method_detail": ""})
        if not targets:
            self._json({"error": "対象ディスクを選択してください"}); return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_name = f"wipe_{timestamp}.log"
        log_file = os.path.join(LOG_DIR, log_name)
        job = {"id": timestamp, "running": True, "stop": False,
            "method": method, "passes": passes, "targets": targets,
            "current_index": 0, "started_at": time.time(),
            "finished_at": None, "log_file": log_name}
        try:
            with open(log_file, "w") as f:
                f.write(f"=== wipe started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                f.write(f"method={method} passes={passes} targets={[t['path'] for t in targets]}\n\n")
        except Exception:
            pass
        job["log_file"] = log_file
        with wipe_lock:
            wipe_job = job
        th = threading.Thread(target=_run_wipe_job, args=(job,), daemon=True)
        th.start()
        self._json({"ok": True, "job_id": timestamp, "log_file": log_name})

    def _handle_wipe_stop(self):
        with wipe_lock:
            if wipe_job is None or not bool(wipe_job.get("running")):
                self._json({"error": "実行中の消去ジョブがありません"}); return
            wipe_job["stop"] = True
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
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"ddrescueGUI running on port {PORT}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        if running_process and running_process.poll() is None:
            os.killpg(os.getpgid(running_process.pid), signal.SIGTERM)
        server.server_close()
