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
import ctypes
from ctypes import wintypes, POINTER, byref, c_void_p, c_uint, c_int, cast
from pathlib import Path
from functools import wraps
from datetime import datetime, date
from io import BytesIO

import requests
import urllib3
import windnd

import tkinter as tk
import customtkinter as ctk
import qrcode
from flask import (
    Flask, request, render_template, jsonify,
    send_file, abort, Response,
)
from PIL import Image, ImageDraw

# 윈도우 작업표시줄과 알림 센터에서 이 프로그램을 별도 앱으로 인식하게 합니다.
try:
    myappid = 'AYA_Share'
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

ole32 = ctypes.windll.ole32
ole32.CoInitialize.argtypes = [c_void_p]
ole32.CoInitialize.restype = ctypes.c_long
_HR = ctypes.c_long

_FOS_ALLOWMULTISELECT = 0x200
_FOS_FORCEFILESYSTEM  = 0x40
_FOS_FILEMUSTEXIST    = 0x1000
_FOS_PATHMUSTEXIST    = 0x800

_CLSID_FileOpenDialog = (wintypes.BYTE * 16)(
    0x9C, 0x5A, 0x1C, 0xDC, 0x8A, 0xE8, 0xDE, 0x4D,
    0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7)
_IID_IFileOpenDialog  = (wintypes.BYTE * 16)(
    0x88, 0x72, 0xC7, 0xD5, 0xAD, 0xD4, 0x68, 0x47,
    0xBE, 0x02, 0x9D, 0x96, 0x95, 0x32, 0xD9, 0x60)

SIGDN_FILESYSPATH = 0x80058000

WF = ctypes.WINFUNCTYPE


def _vfn(ppv, idx, restype, *argtypes):
    """Return a COM vtable method callable with ppv pre-bound."""
    vtbl = cast(c_void_p(ppv.value), POINTER(c_void_p))[0]
    fn = WF(restype, c_void_p, *argtypes)(cast(vtbl, POINTER(c_void_p))[idx])
    return lambda *a: fn(ppv, *a)


def _pick_items(parent_hwnd=0):
    """Windows IFileOpenDialog — selects files AND folders, multi-select."""
    ole32.CoInitialize(None)
    ppv = c_void_p()
    hr = ole32.CoCreateInstance(
        cast(_CLSID_FileOpenDialog, c_void_p), None,
        1, cast(_IID_IFileOpenDialog, c_void_p), byref(ppv))
    if hr != 0:
        return []
    try:
        _vfn(ppv, 9, _HR, c_uint)(
            _FOS_ALLOWMULTISELECT | _FOS_FORCEFILESYSTEM | _FOS_FILEMUSTEXIST | _FOS_PATHMUSTEXIST)
        _vfn(ppv, 17, _HR, wintypes.LPCWSTR)("Select files / folders")
        hr = _vfn(ppv, 3, _HR, c_void_p)(parent_hwnd)
        if hr != 0:
            return []
        psa = c_void_p()
        hr = _vfn(ppv, 20, _HR, POINTER(c_void_p))(byref(psa))
        if hr != 0 or not psa.value:
            return []
        paths = []
        try:
            cnt = c_uint(0)
            _vfn(psa, 3, _HR, POINTER(c_uint))(byref(cnt))
            for i in range(cnt.value):
                psi = c_void_p()
                hr = _vfn(psa, 4, _HR, c_uint, POINTER(c_void_p))(i, byref(psi))
                if hr != 0 or not psi.value:
                    continue
                try:
                    pname = wintypes.LPWSTR()
                    hr = _vfn(psi, 5, _HR, c_int, POINTER(wintypes.LPWSTR))(
                        SIGDN_FILESYSPATH, byref(pname))
                    if hr == 0 and pname.value:
                        paths.append(pname.value)
                        ctypes.windll.ole32.CoTaskMemFree(pname)
                finally:
                    _vfn(psi, 2, _HR)()
            _vfn(psa, 2, _HR)()
        except Exception:
            pass
        return paths
    finally:
        if ppv.value:
            _vfn(ppv, 2, _HR)()
        ole32.CoUninitialize()

# ── Flask App ──────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 * 1024

BASE_DIR = Path(__file__).parent.resolve()
STORAGE_DIR = BASE_DIR / "AYA share"
STORAGE_DIR.mkdir(exist_ok=True)
def _date_dir(base):
    d = base / date.today().isoformat()
    d.mkdir(exist_ok=True)
    return d

# Receive directory: User's Downloads/AYA Share
RECV_DIR = Path(os.path.expanduser("~/Downloads")) / "AYA Share"
RECV_DIR.mkdir(parents=True, exist_ok=True)

# Send directory: User's Downloads/AYA Share (sent from PC)
SENT_DIR = Path(os.path.expanduser("~/Downloads")) / "AYA Share"
SENT_DIR.mkdir(parents=True, exist_ok=True)

TOKEN = None
LAN_IP = None
PORT = 5000

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
    return render_template("index.html", lan_ip=LAN_IP, port=PORT, today=date.today().isoformat())


# ── Windows API 직접 호출을 통한 시스템 알림 표시 ──
def show_native_notification(title, message):
    """윈도우 API를 직접 호출하여 'AYA Share' 이름으로 알림을 표시합니다."""
    try:
        from ctypes import wintypes
        
        # 윈도우 알림에 필요한 구조체 정의
        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HANDLE),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeout", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_char * 16),
                ("hBalloonIcon", wintypes.HANDLE),
            ]

        NIF_INFO = 0x10
        NIF_ICON = 0x02
        NIF_TIP = 0x04
        NIIF_INFO = 0x01
        NIM_ADD = 0x00
        NIM_MODIFY = 0x01
        NIM_DELETE = 0x02

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        
        # Tkinter 메인 윈도우의 실제 시스템 윈도우 핸들을 바인딩
        nid.hWnd = _hwnd_holder["hwnd"]
        nid.uID = 1001
        nid.uFlags = NIF_INFO | NIF_ICON | NIF_TIP
        nid.dwInfoFlags = NIIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        nid.szTip = "AYA Share"
        
        # 시스템 정보 기본 아이콘 로드 (IDI_INFORMATION = 32516)
        nid.hIcon = ctypes.windll.user32.LoadIconW(0, 32516)

        # 알림 요청 전송
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        # 알림이 사용자 화면에 확실히 렌더링되도록 시간 유지 후 트레이 아이콘만 제거
        time.sleep(5)
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    except Exception as e:
        print(f"Notification error: {e}")


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
    threading.Thread(target=show_native_notification, args=("AYA Share", f"파일 수신 완료: {f.filename}"), daemon=True).start()
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

def _clear_send_registry():
    with _send_registry_lock:
        _send_registry.clear()

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
    threading.Thread(target=show_native_notification, args=("AYA Share", f"대용량 전송 완료: {sess['filename']}"), daemon=True).start()
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
    # Canonicalize 'dir' - remove leading/trailing slashes and handle URL decoding
    import urllib.parse
    raw_sub = request.args.get("dir", "")
    sub = urllib.parse.unquote(raw_sub).strip("/").replace("\\", "/")
    
    items = []
    
    # 1. Virtual Shared Registry (The "Shared Files" view)
    # If the user is looking at 'to phone' or the root of sharing, give the registry
    if sub == "to phone" or sub == "":
        registry = _get_send_registry()
        for rel_name, abs_path in registry.items():
            try:
                st = Path(abs_path).stat()
                items.append({
                    "name": rel_name.replace("\\", "/"), 
                    "type": "file", 
                    "size": st.st_size, 
                    "mtime": st.st_mtime
                })
            except Exception as e:
                print(f"Registry stat error: {e}")
        return jsonify({"dir": sub, "items": items})

    # 2. Physical Storage (Received files browsing)
    base = STORAGE_DIR
    if sub:
        # Security: Prevent traversing out of STORAGE_DIR
        if ".." in sub:
            return jsonify({"dir": sub, "items": []})
        base = base / sub
    
    if base.exists() and base.is_dir():
        for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name.startswith("_"): continue
            if p.is_dir():
                items.append({"name": p.name, "type": "dir", "mtime": p.stat().st_mtime})
            else:
                items.append({"name": p.name, "type": "file", "size": p.stat().st_size, "mtime": p.stat().st_mtime})
    
    return jsonify({"dir": sub, "items": items})


@app.route("/download/<path:filename>")
@require_token
def download(filename):
    # Standardize incoming path and handle browser-encoded parts
    # Unquote handles things like %20 and %2F 
    import urllib.parse
    filename = urllib.parse.unquote(filename).replace("\\", "/")
    
    # Check registry first (for "to phone" virtual files)
    if filename.startswith("to phone/"):
        rel = filename[len("to phone/"):]
        registry = _get_send_registry()
        
        # Try finding absolute path using normalized keys
        abs_path_str = registry.get(rel) or registry.get(rel.replace("/", "\\"))
        if not abs_path_str:
            # Last ditch attempt: check for case-insensitive or partial matches
            rel_lower = rel.lower()
            for k, v in registry.items():
                if k.lower() == rel_lower:
                    abs_path_str = v
                    break
                    
        if abs_path_str:
            abs_path = Path(abs_path_str)
            if abs_path.exists() and abs_path.is_file():
                mt, _ = mimetypes.guess_type(str(abs_path))
                return send_file(str(abs_path), mimetype=mt, as_attachment=True, download_name=abs_path.name)
    
    # Fallback to physical storage (Only for received files in AYA share)
    if not filename.startswith("to phone/"):
        full_path = STORAGE_DIR / filename
        if full_path.exists() and full_path.is_file():
            mt, _ = mimetypes.guess_type(str(full_path))
            return send_file(str(full_path), mimetype=mt, as_attachment=True)
    
    abort(404)


@app.route("/api/download_dir")
@require_token
def download_dir():
    import zipfile
    sub = request.args.get("dir", "")
    base = STORAGE_DIR
    if sub:
        base = base / sub
    if not base.exists() or not base.is_dir():
        abort(404)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(str(base)):
            for fn in files:
                fp = os.path.join(root, fn)
                arcname = os.path.relpath(fp, str(base.parent))
                zf.write(fp, arcname)
    buf.seek(0)
    zipname = (sub or "AYA share").replace("/", "_").replace("\\", "_") + ".zip"
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


# ── GUI App ────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class MainApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AYA Share")
        self.geometry("620x610")
        self.minsize(520, 520)

        # Tkinter 내부 시스템 윈도우 핸들을 전역 홀더에 등록
        self.update() # 창의 윈도우 핸들이 활성화되도록 유도
        _hwnd_holder["hwnd"] = self.winfo_id()

        self.selected_files = []
        self.transferring = False
        self._url_visible = False

        self._build_ui()
        self._start_server()
        self._show_server_info()
        self._poll_ips()

    def _toggle_panel(self, open_=None):
        if open_ is None:
            open_ = not self._panel_open
        if open_ == self._panel_open:
            return
        sw = self._panel_width
        sx = -sw if not open_ else 0
        ex = 0 if open_ else -sw
        self._panel_open = open_

        def on_end():
            if open_:
                self.slide_panel.lift()
                self.slide_panel.focus()
                self._build_panel_content()  # refresh dynamic info
                self.bind("<Button-1>", self._close_panel_click, add="+")
            else:
                self.unbind("<Button-1>")
        self._slide_animate(self.slide_panel, sx, ex, steps=24, interval=10, cb=on_end)

    def _close_panel_click(self, e):
        if e.widget != self.slide_panel and not self._is_child_of(e.widget, self.slide_panel) and e.widget != self.menu_btn:
            self._toggle_panel(False)

    def _is_child_of(self, widget, parent):
        while widget:
            if widget == parent:
                return True
            try:
                widget = widget.master
            except Exception:
                break
        return False

    def _build_panel_content(self):
        for widget in self.panel_scroll.winfo_children():
            widget.destroy()

        for cat_name, items in self._panel_categories:
            # Category header
            header = ctk.CTkFrame(self.panel_scroll, fg_color="transparent")
            header.grid(sticky="ew", pady=(12, 4), padx=8)
            header.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(header, text=cat_name,
                          font=ctk.CTkFont(size=11, weight="bold"),
                          text_color="#888").grid(row=0, column=0, sticky="w", padx=4)

            # Category items
            for label, cmd, color in items:
                label_text = label() if callable(label) else label
                if cmd is None:
                    # Info row (non-clickable)
                    row = ctk.CTkLabel(self.panel_scroll, text=label_text,
                                        font=ctk.CTkFont(size=11),
                                        text_color="#666", anchor="w")
                    row.grid(sticky="ew", padx=16, pady=1)
                else:
                    btn = ctk.CTkButton(self.panel_scroll, text=label_text,
                                         fg_color=color, hover_color="#333",
                                         height=30, font=ctk.CTkFont(size=11),
                                         command=lambda c=cmd: (c(), self._toggle_panel(False)))
                    btn.grid(sticky="ew", padx=12, pady=1)

    def _restart_server(self):
        self._start_server()
        self._show_server_info()
        self._log("Server restarted")

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ── Top row: hamburger + server info (left), QR (right) ──
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, pady=(8, 0), sticky="ew")
        top.grid_columnconfigure(1, weight=1)

        self.menu_btn = ctk.CTkButton(top, text="☰", width=28, height=24, fg_color="transparent",
                                        hover_color="#333", command=self._toggle_panel,
                                        font=ctk.CTkFont(size=13))
        self.menu_btn.grid(row=0, column=0, padx=(8, 4), pady=0, sticky="nw")

        info_left = ctk.CTkFrame(top, fg_color="transparent")
        info_left.grid(row=0, column=1, sticky="nsew")
        info_left.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(info_left, text="Server: starting...", anchor="w",
                                          font=ctk.CTkFont(size=11))
        self.status_label.grid(row=0, column=0, padx=(0, 4), pady=(2, 0), sticky="w")

        self.ips_box = ctk.CTkTextbox(info_left, height=18, state="disabled", wrap="none",
                                       font=ctk.CTkFont(size=9), fg_color="transparent",
                                       text_color="#aaa")
        self.ips_box.grid(row=1, column=0, padx=(0, 4), pady=(0, 2), sticky="ew")

        self.qr_label = ctk.CTkLabel(top, text="", width=100, height=100, cursor="hand2")
        self.qr_label.grid(row=0, column=2, padx=(0, 4), pady=2, sticky="ne")
        self.qr_label.bind("<Button-1>", lambda e: self._show_qr_large())

        self.reg_btn = ctk.CTkButton(top, text="⟳", width=28, height=24, fg_color="transparent",
                                       hover_color="#333", command=self._regenerate_token,
                                       font=ctk.CTkFont(size=16))
        self.reg_btn.grid(row=0, column=3, padx=(0, 10), pady=0, sticky="ne")

        # ── Drop zone (CTkButton for reliable click) ──
        _plus_img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        _d = ImageDraw.Draw(_plus_img)
        _d.rectangle((12, 2, 20, 30), fill="#555")
        _d.rectangle((2, 12, 30, 20), fill="#555")
        _plus_ctk = ctk.CTkImage(_plus_img, size=(32, 32))

        self.drop_btn = ctk.CTkButton(
            self, text="drag & drop", image=_plus_ctk, compound="top",
            fg_color="#1e1e1e", hover_color="#2a2a2a",
            corner_radius=8, command=self._show_drop_menu,
            height=120, font=ctk.CTkFont(size=13),
            border_width=2, border_color="#1e1e1e")
        self.drop_btn.grid(row=1, column=0, padx=24, pady=12, sticky="nsew")

        # ── Slide panel (storage) — created LAST for topmost z-order ──
        self._panel_width = 200
        self._panel_open = False
        self.slide_panel = ctk.CTkFrame(self, width=self._panel_width,
                                         fg_color="#181818", corner_radius=0)
        self.slide_panel.place(x=-self._panel_width, y=0, relheight=1)
        self.slide_panel.lift()

        # Scrollable content
        self.panel_scroll = ctk.CTkScrollableFrame(self.slide_panel, fg_color="transparent",
                                                    scrollbar_button_color="#333",
                                                    scrollbar_button_hover_color="#444")
        self.panel_scroll.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.panel_scroll.grid_columnconfigure(0, weight=1)

        # Category data: (name, [(label, command, color), ...])
        # Info category uses dynamic getter
        self._panel_categories = [
            ("📁  Storage", [
                ("받은파일", lambda: os.startfile(str(RECV_DIR)), "#1e1e1e"),
            ]),
            ("⚙️  Actions", [
                ("Regenerate Token", self._regenerate_token, "#1e1e1e"),
                ("Restart Server", self._restart_server, "#1e1e1e"),
                ("Cache Clear", self._confirm_clear, "#555"),
            ]),
            ("ℹ️  Info", [
                (lambda: f"IP: {LAN_IP}", None, "transparent"),
                (lambda: f"Port: {PORT}", None, "transparent"),
            ]),
        ]
        self._build_panel_content()

        # ── Send controls (file list, progress, items) ──
        self.send_frame = ctk.CTkFrame(self)
        self.send_frame.grid(row=2, column=0, padx=12, pady=4, sticky="ew")
        self.send_frame.grid_columnconfigure(0, weight=1)
        self.send_frame.grid_remove()

        btn_row = ctk.CTkFrame(self.send_frame, fg_color="transparent")
        btn_row.grid(row=0, column=0, padx=8, pady=(8, 4), sticky="ew")

        ctk.CTkLabel(btn_row, text="공유 중인 파일",
                      font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ctk.CTkButton(btn_row, text="추가", fg_color="#2563eb", width=60,
                       command=self._show_drop_menu).pack(side="left", padx=(14, 4))
        self.clear_btn = ctk.CTkButton(btn_row, text="공유 전체취소", fg_color="#dc2626",
                                        command=self._clear_files, width=80)
        self.clear_btn.pack(side="right")
        self.file_count = ctk.CTkLabel(btn_row, text="No files", text_color="gray")
        self.file_count.pack(side="right", padx=(0, 10))

        self.file_box = ctk.CTkTextbox(self.send_frame, height=84, state="disabled", wrap="none",
                                        fg_color="#1a1a1a", border_width=1, border_color="#333")
        self.file_box.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")

        self.progress = ctk.CTkProgressBar(self.send_frame)
        self.progress.grid(row=2, column=0, padx=8, pady=(0, 8), sticky="ew")
        self.progress.set(0)

        # ── Activity log ──
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=3, column=0, padx=12, pady=(4, 10), sticky="ew")
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_remove()

        ctk.CTkLabel(log_frame, text="Activity",
                      font=ctk.CTkFont(size=11, weight="bold")).grid(
            row=0, column=0, padx=8, pady=(4, 2), sticky="w")

        self.log_box = ctk.CTkTextbox(log_frame, height=36, state="disabled", wrap="word")
        self.log_box.grid(row=1, column=0, padx=8, pady=(0, 4), sticky="ew")

        # ── Status bar ──
        self.status_bar = ctk.CTkLabel(self, text="", anchor="w",
                                        font=ctk.CTkFont(size=11))
        self.status_bar.grid(row=4, column=0, padx=14, pady=(0, 6), sticky="ew")

        # ── Drag & drop ──
        windnd.hook_dropfiles(self, self._on_files_dropped)
        self.drop_btn.bind("<Enter>", lambda e: self._on_drop_enter(True), add="+")
        self.drop_btn.bind("<Leave>", lambda e: self._on_drop_enter(False), add="+")

    def _on_drop_enter(self, active):
        if active:
            self._animate_to(self.drop_btn, "fg_color", "#1e1e1e", "#333", steps=6)
            self._animate_to(self.drop_btn, "border_color", "#1e1e1e", "#888", steps=6)
        else:
            self._animate_to(self.drop_btn, "fg_color", "#333", "#1e1e1e", steps=6)
            self._animate_to(self.drop_btn, "border_color", "#888", "#1e1e1e", steps=6)

    def _interpolate_color(self, c1, c2, t):
        def norm(c):
            c = c.lstrip("#")
            if len(c) == 3:
                c = "".join(ch * 2 for ch in c)
            return c
        c1, c2 = norm(c1), norm(c2)
        r1, g1, b1 = int(c1[0:2], 16), int(c1[2:4], 16), int(c1[4:6], 16)
        r2, g2, b2 = int(c2[0:2], 16), int(c2[2:4], 16), int(c2[4:6], 16)
        r = int(r1 + (r2 - r1) * t)
        g = int(g1 + (g2 - g1) * t)
        b = int(b1 + (b2 - b1) * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _animate_to(self, widget, prop, from_c, to_c, steps=10, interval=16, cb=None):
        def anim(i):
            if not widget.winfo_exists():
                return
            if i > steps:
                widget.configure(**{prop: to_c})
                if cb: cb()
                return
            t = i / steps
            color = self._interpolate_color(from_c, to_c, t)
            widget.configure(**{prop: color})
            self.after(interval, lambda: anim(i + 1))
        anim(1)

    def _show_overlay(self, build_content, destroy_on_click=True, from_color="#333"):
        overlay = ctk.CTkFrame(self, fg_color=from_color, corner_radius=0)
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        overlay.lift()

        self._animate_to(overlay, "fg_color", from_color, "#111", steps=8, interval=16)

        if destroy_on_click:
            overlay.bind("<Button-1>", lambda e: overlay.destroy())

        content = build_content(overlay)
        return overlay, content

    def _slide_animate(self, widget, start_x, end_x, steps=20, interval=12, cb=None):
        def anim(i):
            if i > steps:
                widget.place(x=end_x)
                if cb: cb()
                return
            t = i / steps
            eased = t * t * (3 - 2 * t)  # smoothstep
            x = int(start_x + (end_x - start_x) * eased)
            widget.place(x=x)
            self.after(interval, lambda: anim(i + 1))
        anim(1)

    def _has_files_or_dirs(self, items):
        for p in items:
            if os.path.isfile(p) or os.path.isdir(p):
                return True
        return False

    def _on_files_dropped(self, paths):
        items = []
        for p in paths:
            try:
                path = p.decode("utf-8") if isinstance(p, bytes) else p
                items.append(path)
            except Exception:
                try:
                    path = p.decode("cp949") if isinstance(p, bytes) else p
                    items.append(path)
                except Exception:
                    pass
        if not self._has_files_or_dirs(items):
            return
        seen = set(self.selected_files)
        new_added = 0
        for p in items:
            if p not in seen:
                self.selected_files.append(p)
                seen.add(p)
                new_added += 1
        if new_added:
            self._refresh_files()
            self._auto_share()
            self._log(f"Auto-shared {new_added} item(s)")

    def _start_server(self):
        global TOKEN, LAN_IP, PORT
        TOKEN = uuid.uuid4().hex + uuid.uuid4().hex
        LAN_IP = get_lan_ip()

        def run():
            try:
                from waitress import serve
                # Increase threads to 100 to handle SSE connections without starving the queue
                serve(app, host="0.0.0.0", port=PORT, threads=100,
                      connection_limit=200, channel_timeout=1800)
            except ImportError:
                app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False,
                        threaded=True)

        threading.Thread(target=run, daemon=True).start()
        time.sleep(0.5)

    def _confirm_clear(self):
        def build(overlay):
            f = ctk.CTkFrame(overlay, fg_color="transparent")
            f.place(relx=0.5, rely=0.45, anchor="center")
            ctk.CTkLabel(f, text="Delete all sent files?",
                          font=ctk.CTkFont(size=14)).pack(pady=(0, 4))
            ctk.CTkLabel(f, text="This cannot be undone.",
                          font=ctk.CTkFont(size=11), text_color="#888").pack(pady=(0, 14))
            btn_row = ctk.CTkFrame(f, fg_color="transparent")
            btn_row.pack()
            ctk.CTkButton(btn_row, text="Yes", fg_color="#dc2626", width=70,
                           command=lambda: self._do_clear_sent(overlay)).pack(side="left", padx=6)
            ctk.CTkButton(btn_row, text="No", fg_color="#555", width=70,
                           command=overlay.destroy).pack(side="left", padx=6)
        self._show_overlay(build, destroy_on_click=True)

    def _do_clear_sent(self, overlay):
        overlay.destroy()
        count = 0
        for d in list(SENT_DIR.iterdir()):
            if not d.is_dir():
                continue
            for p in list(d.iterdir()):
                if p.name.startswith("_"):
                    continue
                try:
                    p.unlink()
                    count += 1
                except Exception:
                    pass
            try:
                d.rmdir()
            except Exception:
                pass
        if count:
            self._log(f"Cleared {count} sent file(s)")
        else:
            self._log("Nothing to clear")

    def _show_server_info(self):
        url = f"http://{LAN_IP}:{PORT}/?token={TOKEN}"

        qr_img = make_qr_image(url)
        qr_img = qr_img.resize((100, 100), Image.NEAREST)
        ctk_img = ctk.CTkImage(light_image=qr_img, dark_image=qr_img, size=(100, 100))
        self.qr_label.configure(image=ctk_img)
        self.qr_label.image = ctk_img

        self.status_label.configure(text=f"Server: Running  ({LAN_IP}:{PORT})", text_color="#22c55e")
        self._log(f"Server started on {LAN_IP}:{PORT}")
        self.status_bar.configure(text=f"Ready on {LAN_IP}:{PORT}")

    def _regenerate_token(self):
        global TOKEN
        TOKEN = uuid.uuid4().hex + uuid.uuid4().hex
        with _device_lock:
            _device_map.clear()
        _clear_send_registry()
        self._show_server_info()
        self._log("Token regenerated")

    def _poll_ips(self):
        try:
            resp = requests.get(f"http://127.0.0.1:{PORT}/api/connected?token={TOKEN}", timeout=2)
            if resp.ok:
                devices = resp.json().get("devices", [])
                self.ips_box.configure(state="normal")
                self.ips_box.delete("0.0", "end")
                if devices:
                    for d in devices:
                        name = d.get("name") or d.get("hostname") or "Unknown"
                        ip = d.get("ip", "")
                        icon = "📱" if name not in ("Windows PC", "Mac", "Unknown") else "💻"
                        self.ips_box.insert("end", f"{icon} {name} ({ip})\n")
                    # remove trailing newline
                    self.ips_box.delete("end-2c", "end-1c")
                else:
                    self.ips_box.insert("end", "No devices connected")
                self.ips_box.configure(state="disabled")
        except Exception:
            pass
        self.after(3000, self._poll_ips)

    def _show_qr_large(self):
        if hasattr(self, '_qr_overlay') and self._qr_overlay.winfo_exists():
            self._qr_overlay.destroy()
            del self._qr_overlay
            return

        def build(overlay):
            url = f"http://{LAN_IP}:{PORT}/?token={TOKEN}"
            qr_img = make_qr_image(url)
            # Ensure compatibility with RGBA conversion
            qr_img = qr_img.convert("RGBA").resize((280, 280), Image.NEAREST)
            ctk_img = ctk.CTkImage(light_image=qr_img, dark_image=qr_img, size=(280, 280))
            
            f = ctk.CTkFrame(overlay, fg_color="transparent")
            f.place(relx=0.5, rely=0.45, anchor="center")
            
            lbl = ctk.CTkLabel(f, text="", image=ctk_img, cursor="hand2")
            lbl.image = ctk_img  # Keep a strong reference
            lbl.pack(pady=(0, 8))
            
            # Stop propagation so clicking the QR doesn't close the overlay
            lbl.bind("<Button-1>", lambda e: "break") 
            
            ctk.CTkLabel(f, text=url, font=ctk.CTkFont(size=10), text_color="#888",
                          wraplength=320).pack()
            ctk.CTkButton(f, text="Close", command=overlay.destroy, fg_color="#555",
                           width=80).pack(pady=(16, 0))
            return f

        overlay, _ = self._show_overlay(build)
        self._qr_overlay = overlay

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _show_drop_menu(self):
        m = tk.Menu(self, tearoff=False, bg="#1a1a1a", fg="#eee",
                     activebackground="#333", activeforeground="#fff",
                     font=ctk.CTkFont(size=12))
        m.add_command(label="📄  Select Files", command=self._pick_files)
        m.add_command(label="📁  Select Folder", command=self._pick_folder)
        try:
            x = self.drop_btn.winfo_rootx()
            y = self.drop_btn.winfo_rooty() + self.drop_btn.winfo_height()
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def _auto_share(self):
        """Register and PUSH shared data to browser immediately."""
        items = self._gather_items()
        _clear_send_registry()
        
        shared_list = []
        if items:
            _register_send_files(items)
            import urllib.parse
            for rel, abs_path in items:
                # Standardize for web
                clean_rel = rel.replace("\\", "/")
                try:
                    st = Path(abs_path).stat()
                    # Capture the direct link
                    link = f"/download/{urllib.parse.quote('to phone/' + clean_rel)}?token={TOKEN}"
                    shared_list.append({
                        "name": clean_rel,
                        "url": link,
                        "size": st.st_size
                    })
                except: pass
                
            if len(items) == 1:
                msg = f"'{items[0][0]}' 파일이 새로 공유되었습니다"
            else:
                msg = f"'{items[0][0]}' 외 {len(items)-1}개의 항목이 새로 공유되었습니다"
        else:
            msg = "공유 목록이 비워졌습니다"
            
        _signal_update(msg, data=shared_list)

    def _on_shared_list_updated(self):
        # Notify SSE
        self._auto_share()

    def _pick_files(self):
        paths = ctk.filedialog.askopenfilenames(title="Select files to send")
        if not paths:
            return
        seen = set(self.selected_files)
        new_count = 0
        for p in paths:
            if p not in seen:
                self.selected_files.append(p)
                seen.add(p)
                new_count += 1
        if new_count:
            self._refresh_files()
            self._auto_share()
            self._log(f"Shared {new_count} file(s)")

    def _pick_folder(self):
        folder = ctk.filedialog.askdirectory(title="Select a folder to send")
        if not folder:
            return
        if folder not in self.selected_files:
            self.selected_files.append(folder)
            self._refresh_files()
            self._auto_share()
            self._log(f"Shared folder: {Path(folder).name}")

    def _clear_files(self):
        self.selected_files = []
        self._refresh_files()
        self._on_shared_list_updated()
        _clear_send_registry()
        _signal_update("공유가 해제되었습니다")
        self._log("All items unshared")

    def _refresh_files(self):
        self.file_box.configure(state="normal")
        self.file_box.delete("0.0", "end")
        if self.selected_files:
            for fp in self.selected_files:
                p = Path(fp)
                if p.is_dir():
                    self.file_box.insert("end", f"📁  {p.name}/\n")
                else:
                    s = p.stat().st_size
                    sz = f"{s}B" if s < 1024 else f"{s/1024:.1f}KB" if s < 1048576 else f"{s/1048576:.1f}MB"
                    self.file_box.insert("end", f"📄  {p.name}  ({sz})\n")
            self.file_count.configure(text=f"{len(self.selected_files)} item(s)")
            self.send_frame.grid()
        else:
            self.file_count.configure(text="No files")
            self.send_frame.grid_remove()
        self.file_box.configure(state="disabled")

    def _gather_items(self):
        items = []
        for sp in self.selected_files:
            p = Path(sp)
            parent = p.parent
            if p.is_dir():
                for root, dirs, fnames in os.walk(str(p)):
                    for fn in fnames:
                        ap = os.path.join(root, fn)
                        rel = os.path.relpath(ap, str(parent))
                        items.append((rel, ap))
            elif p.is_file():
                items.append((p.name, str(p)))
        return items

    def _send_files(self):
        items = self._gather_items()
        if not items or self.transferring:
            return
        total_bytes = sum(Path(a).stat().st_size for _, a in items)
        self.transferring = True
        self.send_btn.configure(state="disabled")
        self.progress.set(0)
        self._log(f"Sending {len(items)} file(s)...")

        modal = SendModal(self, total_bytes)

        def worker():
            total = len(items)
            cancel = False
            # Register files for direct streaming (no copy)
            _register_send_files(items)
            try:
                for i, (rel_path, abs_path) in enumerate(items):
                    if modal.cancelled:
                        cancel = True
                        break
                    fpath = Path(abs_path)
                    if not fpath.exists():
                        self.after(0, lambda n=fpath.name: modal.skip(n))
                        continue
                    try:
                        fsize = fpath.stat().st_size
                        disp_name = rel_path
                        self.after(0, lambda s=fsize: modal.file_ok(s))
                        # Signal web about the new shared file
                        _signal_update(f"{disp_name} shared from PC")
                        self._log(f"  Ready: {disp_name}")
                    except Exception as e:
                        self._log(f"  ERROR: {disp_name} - {e}")
                        self.after(0, modal.file_fail)
                    self.after(0, lambda v=(i+1)/total: self.progress.set(v))
            finally:
                self.after(0, lambda: self._send_done(modal, cancel))

        threading.Thread(target=worker, daemon=True).start()

    def _send_done(self, modal, cancelled):
        self.transferring = False
        self.send_btn.configure(state="normal" if self.selected_files else "disabled")
        self.progress.set(1)
        self._log("All done!")
        modal.finish(cancelled)

    def on_close(self):
        self.destroy()


class SendModal:
    def __init__(self, parent, total_bytes):
        self.parent = parent
        self.cancelled = False
        self.total_bytes = total_bytes
        self.sent_bytes = 0
        self.ok_count = 0
        self.fail_count = 0

        self.overlay = ctk.CTkFrame(parent, fg_color="#111", corner_radius=0)
        self.overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.overlay.lift()

        f = ctk.CTkFrame(self.overlay, fg_color="transparent")
        f.place(relx=0.5, rely=0.5, anchor="center")

        self.status = ctk.CTkLabel(f, text="Preparing...",
                                    font=ctk.CTkFont(size=13))
        self.status.pack(pady=(0, 2))

        self.file_label = ctk.CTkLabel(f, text="",
                                        font=ctk.CTkFont(size=11), text_color="#999")
        self.file_label.pack(pady=(0, 6))

        self.progress = ctk.CTkProgressBar(f, width=280)
        self.progress.pack(pady=(0, 4))
        self.progress.set(0)

        self.pct_label = ctk.CTkLabel(f, text="0%",
                                       font=ctk.CTkFont(size=11), text_color="#888")
        self.pct_label.pack(pady=(0, 6))

        self.cancel_btn = ctk.CTkButton(f, text="Cancel", fg_color="#555",
                                         command=self._cancel, width=80)
        self.cancel_btn.pack(pady=(4, 0))

    def format_size(self, bytes_):
        if bytes_ < 1024:
            return f"{bytes_}B"
        if bytes_ < 1048576:
            return f"{bytes_/1024:.1f}KB"
        return f"{bytes_/1048576:.1f}MB"

    def start_file(self, name, size, idx, total):
        self.status.configure(text=f"File {idx+1} of {total}")
        self.file_label.configure(text=f"{name}  ({self.format_size(size)})")
        if self.total_bytes:
            self.progress.set(self.sent_bytes / max(self.total_bytes, 1))
        self.pct_label.configure(text="Sending...")

    def file_ok(self, size):
        self.sent_bytes += size
        if self.total_bytes:
            pct = self.sent_bytes / max(self.total_bytes, 1)
            self.progress.set(pct)
            self.pct_label.configure(text=f"{pct*100:.1f}%")
        self.ok_count += 1

    def file_fail(self):
        self.fail_count += 1

    def skip(self, name):
        self.file_label.configure(text=f"SKIP: {name}")

    def finish(self, cancelled):
        if cancelled:
            self.status.configure(text="Cancelled")
            self.parent.after(1000, self.overlay.destroy)
        else:
            parts = []
            if self.ok_count:
                parts.append(f"{self.ok_count} ok")
            if self.fail_count:
                parts.append(f"{self.fail_count} failed")
            self.status.configure(text="Done  |  " + ", ".join(parts) if parts else "Done")
            self.parent.after(1500, self.overlay.destroy)

    def _cancel(self):
        self.cancelled = True
        self.status.configure(text="Cancelling...")
        self.cancel_btn.configure(state="disabled")

if __name__ == "__main__":
    app_gui = MainApp()
    app_gui.protocol("WM_DELETE_WINDOW", app_gui.on_close)
    app_gui.mainloop()