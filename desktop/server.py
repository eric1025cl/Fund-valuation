from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request

import uvicorn

from .runtime import DEFAULT_HOST, build_local_url


LOGGER = logging.getLogger(__name__)


def wait_for_health(base_url: str, timeout_seconds: float = 20.0, interval_seconds: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout_seconds
    health_url = base_url.rstrip("/") + "/api/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=2.0) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(interval_seconds)
    return False


class DesktopServer:
    def __init__(self, host: str = DEFAULT_HOST, port: int = 0):
        self.host = host
        self.port = int(port)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return build_local_url(self.port, self.host)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        from app import create_app

        config = uvicorn.Config(
            create_app(enable_scheduler=True),
            host=self.host,
            port=self.port,
            log_level="info",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="fundval-uvicorn", daemon=True)
        self._thread.start()
        LOGGER.info("Started local Fund Valuation service at %s", self.url)

    def wait_until_ready(self, timeout_seconds: float = 20.0) -> bool:
        return wait_for_health(self.url, timeout_seconds=timeout_seconds)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        LOGGER.info("Stopped local Fund Valuation service")
