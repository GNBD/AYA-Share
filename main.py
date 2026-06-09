import os
import sys
import json
import socket
import uuid
import threading
import time
import mimetypes
import shutil
import subprocess
import atexit
import ctypes
from ctypes import wintypes
from pathlib import Path
from functools import wraps
from datetime import datetime, date
from io import BytesIO

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

import urllib3

import qrcode
from flask import (
    Flask, request, render_template, jsonify,
    send_file, abort, Response,
)
from PIL import Image, ImageDraw

# 윈도우 작업표시줄과 알림 센터에서 이 프로그램을 별도 앱으로 인식하게 합니다.
try:
    myappid = 'AYA Share'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except Exception:
    pass

# ── Streaming multipart upload for large files ──
def stream_upload(url, file_path, field_name="file", filename=None, fields=None, timeout=1800):
    """Stream a file via multipart/form-data with chunked encoding. Returns response text."""
    filename = filename or Path(file_path).name
    file_size = Path(file_path).stat().st_size
    boundary = uuid.uuid4().hex
    
    def gen():
        # Headers
        yield f'--{boundary}\r\n'.encode()
        yield f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        yield b'Content-Type: application/octet-stream\r\n\r\n'
        
        # File content in chunks
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)  # 1MB chunks
                if not chunk:
                    break
                yield chunk
        
        # Additional fields
        if fields:
            for k, v in fields.items():
                yield f'\r\n--{boundary}\r\n'.encode()
                yield f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}'.encode()
        
        # End boundary
        yield f'\r\n--{boundary}--\r\n'.encode()
    
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Transfer-Encoding": "chunked",
    }
    
    # Use urllib3 directly for better streaming control
    pool = urllib3.PoolManager()
    resp = pool.request(
        "POST", url,
        body=gen(),
        headers=headers,
        timeout=urllib3.Timeout(connect=10, read=timeout),
        preload_content=False,  # Don't preload - stream response
    )
    return resp.data.decode('utf-8', errors='ignore'), resp.status


# ── Local fast copy (Windows CopyFileEx via shutil, fallback robocopy) ──
def _local_copy_file(src, dst, progress_cb=None):
    """Copy file using Windows native API. Returns (success, error_msg)."""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # shutil.copy2 uses CopyFileEx on Windows (fast, handles large files, preserves metadata)
        import shutil
        shutil.copy2(src, dst)
        if progress_cb:
            progress_cb(dst.stat().st_size)
        return True, None
    except Exception as e:
        # Fallback: robocopy (robust, resume, multi-threaded)
        try:
            import subprocess
            result = subprocess.run(
                ["robocopy", str(src.parent), str(dst.parent), src.name,
                 "/R:3", "/W:1", "/MT:8", "/COPY:DAT", "/NP", "/NFL", "/NDL"],
                capture_output=True, text=True, timeout=3600
            )
            # robocopy exit codes: 0-7 = success, 8+ = error
            if result.returncode <= 7:
                if progress_cb:
                    progress_cb(dst.stat().st_size)
                return True, None
            return False, f"robocopy exit {result.returncode}: {result.stderr}"
        except Exception as e2:
            return False, f"{e}; robocopy: {e2}"


def _is_local_target():
    """Check if send target is this machine (localhost or own LAN IP)."""
    import socket
    try:
        hostname = socket.gethostname()
        local_ips = {ip for ip in socket.gethostbyname_ex(hostname)[2]}
        local_ips.add("127.0.0.1")
        local_ips.add("::1")
        # Also check LAN_IP global
        if 'LAN_IP' in globals():
            local_ips.add(LAN_IP)
        # Target is always 127.0.0.1 in current code
        return True  # We always send to 127.0.0.1
    except Exception:
        return True  # Default to local copy for safety

# ── Flask App ──────────────────────────────────────────────────
app = Flask(__name__, template_folder=resource_path("templates"))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 * 1024

BASE_DIR = Path(__file__).parent.resolve()

# Receive directory: User's Downloads/AYA Share
RECV_DIR = Path(os.path.expanduser("~/Downloads")) / "AYA Share"
RECV_DIR.mkdir(parents=True, exist_ok=True)

# Use the same Downloads folder as the main storage
STORAGE_DIR = RECV_DIR
SHARED_DIR = RECV_DIR / "_shared"

def _date_dir(base):
    d = base / date.today().isoformat()
    d.mkdir(exist_ok=True)
    return d

SENT_DIR = RECV_DIR

TOKEN = None
LAN_IP = None
PORT = 5000

# ── Settings & i18n ──
_CONFIG_DIR = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "AYA Share"
_CONFIG_PATH = _CONFIG_DIR / "config.json"
_LOCALES_DIR = Path(resource_path("locales"))
_LANG_CACHE = {}  # language_code -> dict

def _load_config():
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if _CONFIG_PATH.exists():
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"language": "ko"}

def _save_config(cfg):
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _load_locale(code):
    if code in _LANG_CACHE:
        return _LANG_CACHE[code]
    try:
        p = _LOCALES_DIR / f"{code}.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            _LANG_CACHE[code] = d
            return d
    except Exception:
        pass
    _LANG_CACHE[code] = {}
    return {}

def _t(key, **vars):
    """Translate key -> value with {var} substitution."""
    cfg = _load_config()
    code = cfg.get("language", "ko")
    loc = _load_locale(code)
    val = loc.get(key, key)
    if vars:
        try:
            val = val.format(**vars)
        except Exception:
            pass
    return val

LANGUAGE = _load_config().get("language", "ko")

_file_version = 0
_last_event_msg = None
_last_event_data = None
_version_lock = threading.Lock()
_device_map = {}
_device_lock = threading.Lock()
_hwnd_holder = {"hwnd": 0}  # Tkinter 메인 스레드 창 핸들을 보관할 전역 객체


def _resolve_hostname(ip):
    try:
        h = socket.gethostbyaddr(ip)
        if h and h[0]:
            with _device_lock:
                if ip in _device_map:
                    _device_map[ip]["hostname"] = h[0]
    except Exception:
        pass


def _parse_device_name(ua):
    ua_lower = ua.lower()
    if "iphone" in ua_lower:
        return "iPhone"
    if "ipad" in ua_lower:
        return "iPad"
    if "android" in ua_lower:
        import re
        m = re.search(r'; ([\w\s]+) build/', ua, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r'android.*?; ([^;]+)', ua)
        if m:
            return m.group(1).strip()
        return "Android"
    if "windows" in ua_lower:
        return "Windows PC"
    if "macintosh" in ua_lower or "mac os" in ua_lower:
        return "Mac"
    return None


def _signal_update(msg=None, data=None):
    global _file_version, _last_event_msg, _last_event_data
    with _version_lock:
        _file_version += 1
        _last_event_msg = msg
        _last_event_data = data


def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        tk = request.args.get("token") or request.headers.get("X-Token")
        if tk != TOKEN:
            abort(401)
        ip = request.remote_addr
        if ip and isinstance(ip, str):
            with _device_lock:
                if ip not in _device_map:
                    _device_map[ip] = {"name": None, "ua": request.user_agent.string, "hostname": None}
                    threading.Thread(target=_resolve_hostname, args=(ip,), daemon=True).start()
                elif request.user_agent.string:
                    _device_map[ip]["ua"] = request.user_agent.string
                    if not _device_map[ip]["name"]:
                        _device_map[ip]["name"] = _parse_device_name(request.user_agent.string)
        return f(*args, **kwargs)
    return decorated


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("192.168.0.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def make_qr_image(data):
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.make_image(fill="black", back_color="white")


@app.route("/api/qr")
def api_qr():
    url = f"http://{LAN_IP}:{PORT}/?token={TOKEN}"
    img = make_qr_image(url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/")
@require_token
def index():
    return render_template("index.html", lan_ip=LAN_IP, port=PORT, today=date.today().isoformat(), token=TOKEN)


# ── Windows Toast Notification via PowerShell script file ──
_PS_TOAST_SCRIPT = None

def _get_toast_script_path():
    global _PS_TOAST_SCRIPT
    if _PS_TOAST_SCRIPT is not None:
        return _PS_TOAST_SCRIPT
    import tempfile
    content = (
        "param($Title,$Message)\r\n"
        "$tfm=[Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]\r\n"
        "$xt=[Windows.Data.Xml.Dom.XmlDocument,"
        "Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]\r\n"
        "$x=$xt::new()\r\n"
        "$x.LoadXml(\"<toast><visual><binding template='ToastText02'>"
        "<text id='1'>$Title</text>"
        "<text id='2'>$Message</text>"
        "</binding></visual></toast>\")\r\n"
        "$tt=[Windows.UI.Notifications.ToastNotification,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]\r\n"
        "$t=$tt::new($x)\r\n"
        "$tfm::CreateToastNotifier(\"AYA Share\").Show($t)\r\n"
    )
    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="ayashare_toast_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    _PS_TOAST_SCRIPT = path
    return path

def show_native_notification(title, message):
    try:
        import subprocess
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", _get_toast_script_path(), title, message],
            startupinfo=si, timeout=15, capture_output=True
        )
    except Exception:
        pass


@app.route("/upload", methods=["POST"])
@require_token
def upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "empty filename"}), 400
    rel = request.form.get("path", "")
    dest = _date_dir(RECV_DIR)
    if rel:
        dest = dest / rel
        dest.mkdir(parents=True, exist_ok=True)
    fp = dest / f.filename
    fp.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(fp))
    _signal_update()
    
    # 일반 단일 파일 업로드 완료 시에도 윈도우 시스템 알림 비동기 전송
    threading.Thread(target=show_native_notification, args=(_t("notif_title"), _t("notif_file_received", filename=f.filename)), daemon=True).start()
    return jsonify({"ok": True, "filename": str(Path(rel) / f.filename)})


# ── File registry for "to phone" (virtual files, no copy) ──
_send_registry = {}  # virtual_rel_path -> original_abs_path
_send_registry_lock = threading.Lock()

def _register_send_files(items):
    """Register files for sending. items = [(rel_path, abs_path), ...]"""
    if not items: return
    with _send_registry_lock:
        for rel, abs_path in items:
            # Ensure web-friendly forward slashes for the registry keys
            web_rel = str(rel).replace("\\", "/")
            _send_registry[web_rel] = str(abs_path)

def _unregister_send_files(rels):
    with _send_registry_lock:
        for rel in rels:
            _send_registry.pop(rel, None)

def _cleanup_shared_dir():
    """Remove all files copied into _shared/ (drag-drop uploads)."""
    if SHARED_DIR.exists():
        shutil.rmtree(SHARED_DIR, ignore_errors=True)
    SHARED_DIR.mkdir(parents=True, exist_ok=True)

def _clear_send_registry():
    with _send_registry_lock:
        _send_registry.clear()
    _cleanup_shared_dir()

def _get_send_registry():
    with _send_registry_lock:
        return dict(_send_registry)


# ── Chunked upload for large files (mobile browser) ──
_upload_sessions = {}
_upload_lock = threading.Lock()

def _cleanup_old_uploads(max_age=3600):
    """Remove upload sessions older than max_age seconds."""
    now = time.time()
    with _upload_lock:
        to_del = [uid for uid, s in _upload_sessions.items() if now - s["created"] > max_age]
        for uid in to_del:
            try:
                if s.get("temp_path") and os.path.exists(s["temp_path"]):
                    os.unlink(s["temp_path"])
            except Exception:
                pass
            del _upload_sessions[uid]

@app.route("/api/upload/start", methods=["POST"])
@require_token
def upload_start():
    _cleanup_old_uploads()
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "")
    total_size = data.get("total_size", 0)
    rel_path = data.get("path", "")
    if not filename:
        return jsonify({"error": "filename required"}), 400
    # Create temp file
    temp_dir = _date_dir(RECV_DIR) / ".tmp_uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{uuid.uuid4().hex}.part"
    # Pre-allocate? Not needed, just create empty
    temp_path.touch()
    upload_id = uuid.uuid4().hex
    with _upload_lock:
        _upload_sessions[upload_id] = {
            "filename": filename,
            "total_size": total_size,
            "rel_path": rel_path,
            "temp_path": str(temp_path),
            "received": 0,
            "created": time.time(),
        }
    return jsonify({"ok": True, "upload_id": upload_id})

@app.route("/api/upload/chunk", methods=["POST"])
@require_token
def upload_chunk():
    upload_id = request.form.get("upload_id", "")
    chunk_index = int(request.form.get("chunk_index", "0"))
    total_chunks = int(request.form.get("total_chunks", "1"))
    if not upload_id:
        return jsonify({"error": "upload_id required"}), 400
    with _upload_lock:
        sess = _upload_sessions.get(upload_id)
    if not sess:
        return jsonify({"error": "invalid upload_id"}), 404
    if "file" not in request.files:
        return jsonify({"error": "no chunk"}), 400
    chunk = request.files["file"]
    # Append chunk to temp file and track actual bytes written
    try:
        chunk_data = chunk.read()
        with open(sess["temp_path"], "ab") as f:
            f.write(chunk_data)
        chunk_size = len(chunk_data)
        with _upload_lock:
            sess["received"] += chunk_size
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "received": sess["received"]})

@app.route("/api/upload/finish", methods=["POST"])
@require_token
def upload_finish():
    data = request.get_json(silent=True) or {}
    upload_id = data.get("upload_id", "")
    if not upload_id:
        return jsonify({"error": "upload_id required"}), 400
    with _upload_lock:
        sess = _upload_sessions.pop(upload_id, None)
    if not sess:
        return jsonify({"error": "invalid upload_id"}), 404
    # Move temp file to final destination
    temp_path = Path(sess["temp_path"])
    if not temp_path.exists():
        return jsonify({"error": "temp file missing"}), 500
    dest = _date_dir(RECV_DIR)
    rel = sess["rel_path"]
    if rel:
        dest = dest / rel
        dest.mkdir(parents=True, exist_ok=True)
    fp = dest / sess["filename"]
    fp.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(temp_path), str(fp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    _signal_update()
    
    # 대용량 청크 전송 완료 시 윈도우 시스템 알림 비동기 전송
    threading.Thread(target=show_native_notification, args=(_t("notif_title"), _t("notif_file_received", filename=sess["filename"])), daemon=True).start()
    return jsonify({"ok": True, "filename": str(Path(rel) / sess["filename"])})

@app.route("/api/upload/cancel", methods=["POST"])
@require_token
def upload_cancel():
    data = request.get_json(silent=True) or {}
    upload_id = data.get("upload_id", "")
    if not upload_id:
        return jsonify({"error": "upload_id required"}), 400
    with _upload_lock:
        sess = _upload_sessions.pop(upload_id, None)
    if sess:
        try:
            if sess.get("temp_path") and os.path.exists(sess["temp_path"]):
                os.unlink(sess["temp_path"])
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/files")
@require_token
def list_files():
    import urllib.parse
    raw_sub = request.args.get("dir", "")
    # Normalize and Resolve '..' safely
    sub_raw = urllib.parse.unquote(raw_sub).replace("\\", "/").strip("/")
    parts = []
    for p in sub_raw.split("/"):
        if p == "..":
            if parts: parts.pop()
        elif p and p != ".":
            parts.append(p)
    sub = "/".join(parts)
    
    items = []

    # 1. Received Files (Physical) - Virtual path "received"
    if sub == "received" or sub.startswith("received/"):
        v_prefix = ""
        if sub.startswith("received/"):
            v_prefix = sub[len("received/"):].strip("/")
            
        base = STORAGE_DIR
        if v_prefix:
            base = base / v_prefix
            try: base.relative_to(STORAGE_DIR)
            except: base = STORAGE_DIR

        if base.exists() and base.is_dir():
            for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if p.name.startswith("_"): continue
                if p.is_dir():
                    items.append({"name": p.name, "type": "dir", "mtime": p.stat().st_mtime})
                else:
                    items.append({"name": p.name, "type": "file", "size": p.stat().st_size, "mtime": p.stat().st_mtime})
        return jsonify({"dir": sub, "items": items})

    # 2. Virtual Registry Mapping (Shared Files as ROOT) - handle "to phone" as root
    if sub == "to phone":
        sub = ""  # Treat "to phone" as registry root
    
    registry = _get_send_registry()
    v_prefix = sub + "/" if sub else ""

    visited_dirs = set()
    for rel_name, abs_path in registry.items():
        norm_rel = rel_name.replace("\\", "/").strip("/")
        
        if norm_rel.startswith(v_prefix):
            suffix = norm_rel[len(v_prefix):]
            if not suffix: continue
            
            p_parts = suffix.split("/")
            name = p_parts[0]
            if len(p_parts) > 1:
                if name not in visited_dirs:
                    items.append({"name": name, "type": "dir"})
                    visited_dirs.add(name)
            else:
                try:
                    p_obj = Path(abs_path)
                    if p_obj.exists():
                        items.append({"name": name, "type": "file", "size": p_obj.stat().st_size, "mtime": p_obj.stat().st_mtime})
                except: pass
    
    items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return jsonify({"dir": sub, "items": items})


@app.route("/download/<path:filename>")
@require_token
def download(filename):
    # Standardize incoming path and handle browser-encoded parts
    # Unquote handles things like %20 and %2F 
    import urllib.parse
    filename = urllib.parse.unquote(filename).replace("\\", "/")
    
    # Check registry first (for virtual files)
    registry = _get_send_registry()
    abs_path_str = None
    for k, v in registry.items():
        if k.replace("\\", "/").strip("/") == filename.strip("/"):
            abs_path_str = v
            break
            
    if abs_path_str:
        abs_path = Path(abs_path_str)
        if abs_path.exists() and abs_path.is_file():
            return send_file(str(abs_path), as_attachment=True, download_name=abs_path.name)
    
    # Fallback to physical storage (received files)
    if filename.startswith("received/"):
        rel = filename[len("received/"):].strip("/")
        full_path = STORAGE_DIR / rel
        if full_path.exists() and full_path.is_file():
            mt, _ = mimetypes.guess_type(str(full_path))
            return send_file(str(full_path), mimetype=mt, as_attachment=True)
    
    abort(404)


@app.route("/api/download_dir")
@require_token
def download_dir():
    import zipfile
    sub = request.args.get("dir", "")
    
    buf = BytesIO()
    zipname = (sub or "AYA share").replace("/", "_").replace("\\", "_") + ".zip"

    # 1. Physical Storage (Received Files)
    if sub == "received" or sub.startswith("received/"):
        v_prefix = sub[len("received"):].strip("/")
        base = STORAGE_DIR 
        if v_prefix: base = base / v_prefix
        if not base.exists() or not base.is_dir(): abort(404)
        
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(str(base)):
                for fn in files:
                    fp = os.path.join(root, fn)
                    arcname = os.path.relpath(fp, str(base.parent))
                    zf.write(fp, arcname)
    
    # 2. Virtual Registry Mapping (Shared Folders)
    else:
        registry = _get_send_registry()
        v_prefix = sub.strip("/")
        if v_prefix: v_prefix += "/"
        
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_name, abs_path in registry.items():
                rel_name_norm = rel_name.replace("\\", "/").strip("/")
                if rel_name_norm.startswith(v_prefix):
                    arcname = rel_name_norm[len(v_prefix):]
                    if os.path.exists(abs_path):
                        zf.write(abs_path, arcname)
    
    buf.seek(0)
    resp = send_file(buf, mimetype="application/zip", as_attachment=True, download_name=zipname)
    resp.headers["Content-Length"] = str(buf.getbuffer().nbytes)
    return resp


@app.route("/api/identify")
@app.route("/api/identify", methods=["POST"])
@require_token
def api_identify():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    device_type = (data.get("type") or "").strip()
    ip = request.remote_addr
    if name and ip and isinstance(ip, str):
        with _device_lock:
            if ip in _device_map:
                _device_map[ip]["name"] = name
            else:
                _device_map[ip] = {"name": name, "ua": device_type, "hostname": None}
    return jsonify({"ok": True})


@app.route("/api/send", methods=["POST"])
@require_token
def api_send():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "empty filename"}), 400
    rel = request.form.get("path", "")
    dest = _date_dir(SENT_DIR)
    if rel:
        dest = dest / rel
        dest.mkdir(parents=True, exist_ok=True)
    fp = dest / f.filename
    fp.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(fp))
    _signal_update()
    return jsonify({"ok": True, "filename": str(Path(rel) / f.filename)})


@app.route("/api/connected")
@require_token
def api_connected():
    with _device_lock:
        devices = [{"ip": ip, "name": info.get("name") or info.get("hostname") or "Unknown"} for ip, info in sorted(_device_map.items())]
    return jsonify({"devices": devices})


@app.route("/api/events")
@require_token
def events():
    last = _file_version
    import urllib.parse
    def generate():
        nonlocal last
        count = 0
        while True:
            with _version_lock:
                cur = _file_version
                msg = _last_event_msg
                data = _last_event_data
            if cur != last:
                last = cur
                yield f"data: {json.dumps({'type': 'update', 'msg': msg, 'items': data})}\n\n"
            else:
                # Heartbeat every 20 seconds to prevent timeout
                count += 1
                if count >= 20:
                    yield ": heartbeat\n\n"
                    count = 0
            time.sleep(1)
    return Response(generate(), mimetype="text/event-stream")


# ── Admin API for NiceGUI ─────────────────────────────────────
@app.route("/api/admin/status")
@require_token
def api_admin_status():
    return jsonify({
        "lan_ip": LAN_IP,
        "port": PORT,
        "token": TOKEN,
        "server_running": True,
        "recv_dir": str(RECV_DIR),
        "shared_count": len(_get_send_registry()),
    })


@app.route("/api/admin/regenerate", methods=["POST"])
@require_token
def api_admin_regenerate():
    global TOKEN
    TOKEN = uuid.uuid4().hex + uuid.uuid4().hex
    with _device_lock:
        _device_map.clear()
    _clear_send_registry()
    return jsonify({"ok": True, "token": TOKEN})


@app.route("/api/admin/restart", methods=["POST"])
@require_token
def api_admin_restart():
    threading.Thread(target=lambda: (
        time.sleep(0.1),
        os._exit(1) if 'BUNDLED' in dir() else os.execl(sys.executable, sys.executable, *sys.argv)
    ), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/admin/info")
@require_token
def api_admin_info():
    reg = _get_send_registry()
    items = []
    visited_dirs = set()
    for rel, abs_path in reg.items():
        norm = rel.replace("\\", "/").strip("/")
        parts = norm.split("/")
        name = parts[0]
        if len(parts) > 1:
            if name not in visited_dirs:
                items.append({"name": name + "/", "type": "dir", "size": 0})
                visited_dirs.add(name)
        else:
            try:
                p = Path(abs_path)
                items.append({"name": name, "type": "file", "size": p.stat().st_size if p.exists() else 0})
            except:
                items.append({"name": name, "type": "file", "size": 0})
    items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return jsonify({"shared_files": items})


@app.route("/api/admin/register", methods=["POST"])
@require_token
def api_admin_register():
    data = request.get_json(silent=True) or {}
    paths = data.get("paths", [])
    items = []
    for sp in paths:
        p = Path(sp)
        parent = p.parent
        if not p.exists():
            continue
        if p.is_dir():
            for root, dirs, fnames in os.walk(str(p)):
                for fn in fnames:
                    ap = os.path.join(root, fn)
                    rel = os.path.relpath(ap, str(parent))
                    items.append((rel, ap))
        elif p.is_file():
            items.append((p.name, str(p)))
    _clear_send_registry()
    _register_send_files(items)
    _signal_update()
    result = []
    for rel, ap in items:
        try:
            pobj = Path(ap)
            result.append({"name": rel, "size": pobj.stat().st_size if pobj.exists() else 0})
        except:
            result.append({"name": rel, "size": 0})
    return jsonify({"ok": True, "count": len(items), "items": result})


@app.route("/api/admin/clear", methods=["POST"])
@require_token
def api_admin_clear():
    _clear_send_registry()
    _signal_update(_t("msg_shared_cancelled"))
    return jsonify({"ok": True})


@app.route("/api/admin/share_upload", methods=["POST"])
@require_token
def api_admin_share_upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    files = request.files.getlist("file")
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    items = []
    for f in files:
        if not f.filename:
            continue
        total_bytes += f.content_length or 0
    MAX_BYTES = 10 * 1024 * 1024 * 1024
    if total_bytes > MAX_BYTES:
        return jsonify({"error": "total size exceeds 10GB limit"}), 413
    for f in files:
        if not f.filename:
            continue
        safe_name = f.filename.replace("\\", "/").strip("/")
        fp = SHARED_DIR / safe_name
        fp.parent.mkdir(parents=True, exist_ok=True)
        f.save(str(fp))
        items.append((safe_name, str(fp)))
    if items:
        _register_send_files(items)
        _signal_update()
    return jsonify({"ok": True, "count": len(items)})


@app.route("/api/admin/recv_clear", methods=["POST"])
@require_token
def api_admin_recv_clear():
    count = 0
    for p in list(RECV_DIR.iterdir()):
        if p.name.startswith("_"):
            continue
        try:
            if p.is_file():
                p.unlink()
                count += 1
            elif p.is_dir():
                shutil.rmtree(p)
                count += 1
        except Exception:
            pass
    _signal_update()
    return jsonify({"ok": True, "count": count})


def _remove_temp_path(abs_path):
    """Remove a file/dir if it's under SHARED_DIR (drag-drop copy)."""
    try:
        p = Path(abs_path)
        if str(p).startswith(str(SHARED_DIR.resolve())):
            if p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass

@app.route("/api/admin/unregister", methods=["POST"])
@require_token
def api_admin_unregister():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    norm = name.replace("\\", "/").strip("/")
    removed_paths = []
    with _send_registry_lock:
        if norm in _send_registry:
            removed_paths.append(_send_registry.pop(norm, None))
        else:
            prefix = norm.rstrip("/") + "/"
            to_remove = [k for k in _send_registry if k.startswith(prefix) or k.replace("\\", "/").strip("/").startswith(prefix)]
            for k in to_remove:
                removed_paths.append(_send_registry.pop(k, None))
    for p in removed_paths:
        _remove_temp_path(p)
    _signal_update()
    return jsonify({"ok": True})


@app.route("/api/admin/recv_open", methods=["POST"])
@require_token
def api_admin_recv_open():
    try:
        os.startfile(str(RECV_DIR))
    except Exception:
        pass
    return jsonify({"ok": True})


# ── Language API ──
@app.route("/api/language", methods=["GET"])
def api_language_get():
    cfg = _load_config()
    code = cfg.get("language", "ko")
    loc = _load_locale(code)
    return jsonify({"language": code, "strings": loc})

@app.route("/api/language", methods=["POST"])
def api_language_set():
    data = request.get_json(silent=True) or {}
    code = data.get("language", "ko")
    if code not in ("ko", "en"):
        return jsonify({"ok": False, "error": "unsupported language"}), 400
    _save_config({"language": code})
    global LANGUAGE
    LANGUAGE = code
    _LANG_CACHE.pop(code, None)
    loc = _load_locale(code)
    return jsonify({"ok": True, "language": code, "strings": loc})

# ── Desktop UI Route ──
@app.route("/admin/desktop")
def admin_desktop():
    return render_template("desktop.html", token=TOKEN or "", lan_ip=LAN_IP or "127.0.0.1", port=PORT, language=LANGUAGE)


# ── Entry Point: Launch Flask + PyWebview ──────────────────────
def start():
    global LAN_IP, TOKEN
    TOKEN = uuid.uuid4().hex + uuid.uuid4().hex
    LAN_IP = get_lan_ip()

    def run_flask():
        try:
            from waitress import serve
            serve(app, host="0.0.0.0", port=PORT, threads=100,
                  connection_limit=200, channel_timeout=1800)
        except ImportError:
            app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False,
                    threaded=True)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(1.0)

    # Initialize _shared dir
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    atexit.register(_cleanup_shared_dir)

    # Cleanup old temp/upload dirs (legacy)
    for name in (".tmp_uploads", ".nicegui_uploads"):
        for d in RECV_DIR.rglob(name):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)

    from gui_pywebview import run_gui
    run_gui(LAN_IP, TOKEN, PORT, _hwnd_holder)

    # App closed: clean up temp copy dir
    _cleanup_shared_dir()


if __name__ == "__main__":
    start()