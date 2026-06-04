"""
AutoPOI Desktop App — PyWebView launcher
Chạy: python src/desktop.py
"""

import sys
import threading
import time
import socket
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn
import webview

# ── Tìm port trống ─────────────────────────────────────────────────────────
def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Chạy FastAPI trong background thread ────────────────────────────────────
def start_server(port: int):
    """Khởi động FastAPI server trong thread riêng."""
    from src.app import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


def wait_for_server(port: int, timeout: float = 10.0):
    """Chờ server sẵn sàng trước khi mở cửa sổ."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    port = find_free_port()

    # Khởi động server trong background
    server_thread = threading.Thread(
        target=start_server,
        args=(port,),
        daemon=True,   # Tự tắt khi cửa sổ đóng
    )
    server_thread.start()

    # Chờ server ready
    if not wait_for_server(port):
        print("[ERROR] Server khong khoi dong duoc!")
        sys.exit(1)

    # Mở cửa sổ desktop native
    webview.create_window(
        title="AutoPOI — Venue Data Assistant",
        url=f"http://127.0.0.1:{port}",
        width=920,
        height=820,
        min_size=(720, 600),
        resizable=True,
        text_select=True,       # Cho phép select text để copy
        confirm_close=False,
    )

    webview.start(debug=False)


if __name__ == "__main__":
    main()
