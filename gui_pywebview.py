import threading
import webview


class DesktopApi:
    """Exposed to JS via pywebview js_api bridge."""

    def pick_files(self):
        w = webview.active_window()
        if not w:
            return []
        result = w.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True)
        return list(result) if result else []

    def pick_folder(self):
        w = webview.active_window()
        if not w:
            return None
        result = w.create_file_dialog(webview.FileDialog.FOLDER)
        return result[0] if result else None


def run_gui(lan_ip, token, port, hwnd_holder=None):
    api = DesktopApi()
    url = f"http://127.0.0.1:{port}/admin/desktop"

    window = webview.create_window(
        "AYA Share",
        url=url,
        js_api=api,
        width=680,
        height=760,
        min_size=(560, 600),
        resizable=True,
    )

    # Start a thread to capture the native HWND once the window is shown
    def _capture_hwnd():
        if hwnd_holder is None:
            return
        try:
            window.events.shown.wait(timeout=10)
            n = getattr(window, 'native', None)
            if n is not None:
                import clr
                hwnd = n.Handle.ToInt32()
                hwnd_holder["hwnd"] = hwnd
        except Exception:
            pass

    threading.Thread(target=_capture_hwnd, daemon=True).start()
    webview.start()
