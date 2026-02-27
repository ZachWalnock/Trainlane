from __future__ import annotations

import socket
import threading
import time

import uvicorn

from expkit.dashboard.app import create_dashboard_app
from expkit.db import Store


class DashboardServer:
    def __init__(self, store: Store, host: str = "127.0.0.1", port: int = 8421):
        self.store = store
        self.host = host
        self.port = port
        self._thread: threading.Thread | None = None
        self._started = False

    def _find_port(self, preferred: int) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((self.host, preferred))
                return preferred
            except OSError:
                sock.bind((self.host, 0))
                return sock.getsockname()[1]

    def start(self) -> int:
        if self._started:
            return self.port

        self.port = self._find_port(self.port)
        app = create_dashboard_app(self.store)

        def _serve() -> None:
            uvicorn.run(
                app,
                host=self.host,
                port=self.port,
                log_level="warning",
                access_log=False,
            )

        self._thread = threading.Thread(target=_serve, daemon=True)
        self._thread.start()
        self._wait_until_ready(timeout_seconds=8.0)
        self._started = True
        return self.port

    def _wait_until_ready(self, timeout_seconds: float) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.25)
                if sock.connect_ex((self.host, self.port)) == 0:
                    return
            time.sleep(0.05)

        raise RuntimeError(
            f"Dashboard server did not start on {self.host}:{self.port} within {timeout_seconds}s."
        )
