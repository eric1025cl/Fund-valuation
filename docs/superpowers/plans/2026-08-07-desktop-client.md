# Desktop Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fully local Windows/macOS desktop client that preserves the existing Fund Valuation web app and FastAPI behavior.

**Architecture:** Add a small `desktop/` package that starts the existing FastAPI app on a free loopback port, waits for `/api/health`, and opens the existing web UI in a pywebview window. Make the backend data directory configurable so packaged desktop builds write SQLite data to a stable local user-data path instead of a bundle extraction directory.

**Tech Stack:** Python, FastAPI, Uvicorn, pywebview, PyInstaller, unittest, PowerShell, POSIX shell.

---

## File Structure

- Create `desktop/__init__.py`: marks the desktop launcher package.
- Create `desktop/runtime.py`: pure helper functions for port allocation, URL construction, packaged/non-packaged data paths, log paths, and environment setup.
- Create `desktop/server.py`: managed Uvicorn wrapper that starts `app.create_app()` in a background thread, waits for health, and shuts down cleanly.
- Create `desktop/main.py`: user-facing desktop entry point that configures logging, starts the managed server, opens pywebview, and shows startup failures.
- Create `desktop/__main__.py`: enables `python -m desktop`.
- Create `desktop/build-windows.ps1`: Windows PyInstaller build script.
- Create `desktop/build-macos.sh`: macOS PyInstaller build script.
- Create `tests/test_desktop_runtime.py`: unit tests for deterministic runtime helpers.
- Create `tests/test_desktop_server.py`: unit tests for server health polling using a local HTTP server, without opening a desktop window.
- Modify `app.py`: allow `FUNDVAL_DATA_DIR` or an explicit `data_dir` argument to control SQLite storage.
- Modify `requirements.txt`: add desktop runtime and packaging dependencies.
- Modify `README.md`: document desktop development run, Windows build, macOS build, and local data location behavior.

## Task 1: Make the backend data directory configurable

**Files:**
- Modify: `app.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: Add a failing API test for explicit data directory injection**

Append this test method inside `ApiTests` in `tests/test_api.py`:

```python
    def test_create_app_accepts_explicit_data_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "desktop-data"
            app = create_app(enable_scheduler=False, data_dir=data_dir)
            store = app.state.valuation_service.store

        self.assertEqual(store.db_path, data_dir / "funds.db")
```

- [ ] **Step 2: Run the failing test**

Run:

```powershell
python -m unittest tests.test_api.ApiTests.test_create_app_accepts_explicit_data_dir -v
```

Expected: FAIL with `TypeError: create_app() got an unexpected keyword argument 'data_dir'`.

- [ ] **Step 3: Implement the configurable data directory**

In `app.py`, add `import os` and update `create_app()` plus `_default_service()` to this shape:

```python
from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fundval.providers import AkshareProvider
from fundval.service import FundValuationService
from fundval.store import WatchlistStore


ROOT_DIR = Path(__file__).resolve().parent
WEB_DIR = ROOT_DIR / "web"
DATA_DIR = ROOT_DIR / "data"
DATA_DIR_ENV = "FUNDVAL_DATA_DIR"
```

Update the function signatures and service creation:

```python
def create_app(
    service: FundValuationService | None = None,
    enable_scheduler: bool | None = None,
    data_dir: Path | str | None = None,
) -> FastAPI:
    valuation_service = service or _default_service(data_dir=data_dir)
    should_schedule = (service is None) if enable_scheduler is None else enable_scheduler
    api = FastAPI(title="Fund Valuation", version="0.1.0")
    api.state.valuation_service = valuation_service
```

Replace `_default_service()` with:

```python
def _default_service(data_dir: Path | str | None = None) -> FundValuationService:
    resolved_data_dir = Path(data_dir or os.environ.get(DATA_DIR_ENV) or DATA_DIR)
    store = WatchlistStore(resolved_data_dir / "funds.db")
    provider = AkshareProvider()
    return FundValuationService(store=store, provider=provider)
```

Leave the existing route definitions, scheduler setup, and `app = create_app()` in place.

- [ ] **Step 4: Run the focused test**

Run:

```powershell
python -m unittest tests.test_api.ApiTests.test_create_app_accepts_explicit_data_dir -v
```

Expected: PASS.

- [ ] **Step 5: Run existing API tests**

Run:

```powershell
python -m unittest tests.test_api -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```powershell
git add app.py tests/test_api.py
git commit -m "Allow configurable valuation data directory"
```

## Task 2: Add testable desktop runtime helpers

**Files:**
- Create: `desktop/__init__.py`
- Create: `desktop/runtime.py`
- Test: `tests/test_desktop_runtime.py`

- [ ] **Step 1: Write failing runtime helper tests**

Create `tests/test_desktop_runtime.py`:

```python
import os
import socket
import tempfile
import unittest
from pathlib import Path

from desktop.runtime import (
    DATA_DIR_ENV,
    build_local_url,
    configure_desktop_environment,
    find_free_port,
    resolve_app_data_dir,
    resolve_log_file,
)


class DesktopRuntimeTests(unittest.TestCase):
    def test_build_local_url_uses_loopback_and_port(self):
        self.assertEqual(build_local_url(8123), "http://127.0.0.1:8123/")

    def test_find_free_port_returns_bindable_port(self):
        port = find_free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))

    def test_source_run_uses_project_data_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            data_dir = resolve_app_data_dir(root=root, frozen=False, platform="win32", env={}, home=root)

        self.assertEqual(data_dir, root / "data")

    def test_windows_packaged_run_uses_local_app_data(self):
        env = {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}

        data_dir = resolve_app_data_dir(
            root=Path(r"C:\bundle"),
            frozen=True,
            platform="win32",
            env=env,
            home=Path(r"C:\Users\me"),
        )

        self.assertEqual(data_dir, Path(env["LOCALAPPDATA"]) / "FundValuation" / "data")

    def test_macos_packaged_run_uses_application_support(self):
        data_dir = resolve_app_data_dir(
            root=Path("/Applications/FundValuation.app"),
            frozen=True,
            platform="darwin",
            env={},
            home=Path("/Users/me"),
        )

        self.assertEqual(data_dir, Path("/Users/me/Library/Application Support/FundValuation/data"))

    def test_environment_override_wins_for_data_dir(self):
        env = {DATA_DIR_ENV: r"D:\fund-data"}

        data_dir = resolve_app_data_dir(
            root=Path(r"C:\repo"),
            frozen=True,
            platform="win32",
            env=env,
            home=Path(r"C:\Users\me"),
        )

        self.assertEqual(data_dir, Path(env[DATA_DIR_ENV]))

    def test_configure_desktop_environment_sets_data_dir_and_log_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            previous_data = os.environ.get(DATA_DIR_ENV)
            previous_log = os.environ.get("FUNDVAL_DESKTOP_LOG")
            try:
                result = configure_desktop_environment(root=root, frozen=False)

                self.assertEqual(os.environ[DATA_DIR_ENV], str(root / "data"))
                self.assertEqual(os.environ["FUNDVAL_DESKTOP_LOG"], str(root / "logs" / "desktop.log"))
                self.assertEqual(result.data_dir, root / "data")
                self.assertEqual(result.log_file, root / "logs" / "desktop.log")
            finally:
                if previous_data is None:
                    os.environ.pop(DATA_DIR_ENV, None)
                else:
                    os.environ[DATA_DIR_ENV] = previous_data
                if previous_log is None:
                    os.environ.pop("FUNDVAL_DESKTOP_LOG", None)
                else:
                    os.environ["FUNDVAL_DESKTOP_LOG"] = previous_log

    def test_resolve_log_file_places_log_next_to_runtime_data_parent(self):
        data_dir = Path("/Users/me/Library/Application Support/FundValuation/data")

        log_file = resolve_log_file(data_dir)

        self.assertEqual(log_file, data_dir.parent / "logs" / "desktop.log")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the failing runtime tests**

Run:

```powershell
python -m unittest tests.test_desktop_runtime -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'desktop'`.

- [ ] **Step 3: Create the desktop package and runtime helpers**

Create `desktop/__init__.py`:

```python
"""Desktop launcher package for Fund Valuation."""
```

Create `desktop/runtime.py`:

```python
from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


APP_DIR_NAME = "FundValuation"
DATA_DIR_ENV = "FUNDVAL_DATA_DIR"
DESKTOP_LOG_ENV = "FUNDVAL_DESKTOP_LOG"
DEFAULT_HOST = "127.0.0.1"


@dataclass(frozen=True)
class DesktopEnvironment:
    data_dir: Path
    log_file: Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def build_local_url(port: int, host: str = DEFAULT_HOST) -> str:
    return f"http://{host}:{int(port)}/"


def find_free_port(host: str = DEFAULT_HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def resolve_app_data_dir(
    root: Path | None = None,
    frozen: bool | None = None,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env_values = os.environ if env is None else env
    if env_values.get(DATA_DIR_ENV):
        return Path(env_values[DATA_DIR_ENV])

    effective_root = root or project_root()
    effective_frozen = is_frozen_app() if frozen is None else frozen
    effective_platform = platform or sys.platform
    effective_home = home or Path.home()

    if not effective_frozen:
        return effective_root / "data"

    if effective_platform == "win32":
        local_app_data = env_values.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_DIR_NAME / "data"
        return effective_home / "AppData" / "Local" / APP_DIR_NAME / "data"

    if effective_platform == "darwin":
        return effective_home / "Library" / "Application Support" / APP_DIR_NAME / "data"

    return effective_home / ".local" / "share" / APP_DIR_NAME / "data"


def resolve_log_file(data_dir: Path) -> Path:
    return data_dir.parent / "logs" / "desktop.log"


def configure_desktop_environment(root: Path | None = None, frozen: bool | None = None) -> DesktopEnvironment:
    data_dir = resolve_app_data_dir(root=root, frozen=frozen)
    log_file = resolve_log_file(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    os.environ[DATA_DIR_ENV] = str(data_dir)
    os.environ[DESKTOP_LOG_ENV] = str(log_file)
    return DesktopEnvironment(data_dir=data_dir, log_file=log_file)
```

- [ ] **Step 4: Run the runtime tests**

Run:

```powershell
python -m unittest tests.test_desktop_runtime -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```powershell
git add desktop/__init__.py desktop/runtime.py tests/test_desktop_runtime.py
git commit -m "Add desktop runtime helpers"
```

## Task 3: Add managed Uvicorn server wrapper

**Files:**
- Create: `desktop/server.py`
- Test: `tests/test_desktop_server.py`

- [ ] **Step 1: Write failing tests for health polling and URL state**

Create `tests/test_desktop_server.py`:

```python
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from desktop.server import DesktopServer, wait_for_health


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/health":
            payload = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


class DesktopServerTests(unittest.TestCase):
    def test_wait_for_health_returns_true_when_endpoint_responds(self):
        httpd = HTTPServer(("127.0.0.1", 0), HealthHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{httpd.server_port}/"

            self.assertTrue(wait_for_health(url, timeout_seconds=2.0, interval_seconds=0.05))
        finally:
            httpd.shutdown()
            thread.join(timeout=2)

    def test_wait_for_health_returns_false_when_endpoint_never_responds(self):
        start = time.monotonic()

        healthy = wait_for_health("http://127.0.0.1:9/", timeout_seconds=0.2, interval_seconds=0.05)

        self.assertFalse(healthy)
        self.assertGreaterEqual(time.monotonic() - start, 0.15)

    def test_desktop_server_exposes_url(self):
        server = DesktopServer(host="127.0.0.1", port=8123)

        self.assertEqual(server.url, "http://127.0.0.1:8123/")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the failing server tests**

Run:

```powershell
python -m unittest tests.test_desktop_server -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'desktop.server'`.

- [ ] **Step 3: Implement the managed server wrapper**

Create `desktop/server.py`:

```python
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
```

- [ ] **Step 4: Run the server tests**

Run:

```powershell
python -m unittest tests.test_desktop_server -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```powershell
git add desktop/server.py tests/test_desktop_server.py
git commit -m "Add managed desktop server wrapper"
```

## Task 4: Add the desktop window entry point

**Files:**
- Create: `desktop/main.py`
- Create: `desktop/__main__.py`
- Test: manual import/compile checks

- [ ] **Step 1: Create the desktop main entry point**

Create `desktop/main.py`:

```python
from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

from .runtime import configure_desktop_environment, find_free_port
from .server import DesktopServer


WINDOW_TITLE = "Fund Valuation"
STARTUP_TIMEOUT_SECONDS = 30.0


def configure_logging(log_file: Path) -> None:
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def show_startup_error(message: str, log_file: Path) -> None:
    full_message = f"{message}\n\nLog file: {log_file}"
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Fund Valuation startup failed", full_message)
        root.destroy()
    except Exception:
        print(full_message, file=sys.stderr)


def run_desktop() -> int:
    env = configure_desktop_environment()
    configure_logging(env.log_file)
    server: DesktopServer | None = None

    try:
        import webview

        port = find_free_port()
        server = DesktopServer(port=port)
        server.start()
        if not server.wait_until_ready(timeout_seconds=STARTUP_TIMEOUT_SECONDS):
            raise RuntimeError(f"Local service did not become healthy at {server.url}")

        webview.create_window(
            WINDOW_TITLE,
            server.url,
            width=1280,
            height=860,
            min_size=(1024, 700),
        )
        webview.start()
        return 0
    except Exception as exc:
        logging.error("Desktop startup failed:\n%s", traceback.format_exc())
        show_startup_error(str(exc), env.log_file)
        return 1
    finally:
        if server is not None:
            server.stop()


def main() -> None:
    raise SystemExit(run_desktop())


if __name__ == "__main__":
    main()
```

Create `desktop/__main__.py`:

```python
from .main import main


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run compile checks**

Run:

```powershell
python -m py_compile desktop/runtime.py desktop/server.py desktop/main.py desktop/__main__.py app.py
```

Expected: command exits with code 0.

- [ ] **Step 3: Verify module entry point reaches dependency error clearly before pywebview is installed**

Run:

```powershell
python -m desktop
```

Expected before adding requirements or installing `pywebview`: a startup error dialog or stderr message containing `No module named 'webview'` and a log file path under `logs/desktop.log`.

- [ ] **Step 4: Commit**

Run:

```powershell
git add desktop/main.py desktop/__main__.py
git commit -m "Add desktop window entry point"
```

## Task 5: Add desktop dependencies and verify local desktop startup

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add dependencies**

Append these lines to `requirements.txt`:

```text
pywebview
pyinstaller
```

- [ ] **Step 2: Install dependencies**

Run:

```powershell
python -m pip install -r requirements.txt
```

Expected: command exits with code 0 and installs `pywebview` plus `pyinstaller`.

- [ ] **Step 3: Run full Python and frontend checks before desktop smoke**

Run:

```powershell
python -m unittest discover -s tests -v
node --check web\app.js
python -m py_compile app.py desktop/runtime.py desktop/server.py desktop/main.py desktop/__main__.py
```

Expected: all commands pass.

- [ ] **Step 4: Run Windows desktop smoke test**

Run:

```powershell
python -m desktop
```

Expected:

- A desktop window titled `Fund Valuation` opens.
- The page at `/` renders the existing fund valuation UI.
- `logs/desktop.log` exists.
- Closing the desktop window exits the process.

- [ ] **Step 5: Commit**

Run:

```powershell
git add requirements.txt
git commit -m "Add desktop packaging dependencies"
```

## Task 6: Add PyInstaller build scripts

**Files:**
- Create: `desktop/build-windows.ps1`
- Create: `desktop/build-macos.sh`

- [ ] **Step 1: Create the Windows build script**

Create `desktop/build-windows.ps1`:

```powershell
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $RepoRoot

python -m pip install -r requirements.txt

python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name FundValuation `
  --add-data "web;web" `
  --hidden-import app `
  --hidden-import fundval.providers `
  --hidden-import fundval.service `
  --hidden-import fundval.store `
  --hidden-import fundval.valuation `
  --collect-all akshare `
  --collect-all pandas `
  desktop/main.py

Write-Host "Windows desktop build created under dist\FundValuation"
```

- [ ] **Step 2: Create the macOS build script**

Create `desktop/build-macos.sh`:

```sh
#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

python3 -m pip install -r requirements.txt

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name FundValuation \
  --add-data "web:web" \
  --hidden-import app \
  --hidden-import fundval.providers \
  --hidden-import fundval.service \
  --hidden-import fundval.store \
  --hidden-import fundval.valuation \
  --collect-all akshare \
  --collect-all pandas \
  desktop/main.py

echo "macOS desktop build created under dist/FundValuation.app"
```

- [ ] **Step 3: Make the macOS script executable if the filesystem supports it**

Run:

```powershell
git update-index --chmod=+x desktop/build-macos.sh
```

Expected: command exits with code 0.

- [ ] **Step 4: Run static checks for the build scripts**

Run:

```powershell
Get-Content desktop\build-windows.ps1 -Encoding utf8 | Select-String -Pattern "PyInstaller","--add-data `"web;web`"","--hidden-import app"
Get-Content desktop\build-macos.sh -Encoding utf8 | Select-String -Pattern "PyInstaller","--add-data `"web:web`"","--hidden-import app"
```

Expected: each `Select-String` prints matching lines.

- [ ] **Step 5: Commit**

Run:

```powershell
git add desktop/build-windows.ps1 desktop/build-macos.sh
git commit -m "Add desktop build scripts"
```

## Task 7: Update README with desktop usage

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add desktop run and packaging documentation**

Insert this section after the existing startup section in `README.md`:

````markdown
## 桌面客户端

桌面版是完全本地单机应用。启动后会在本机自动拉起内置 FastAPI 服务，并在桌面窗口中加载现有页面；自选基金、快照和对账数据仍保存在本机 SQLite。

开发环境启动：

```powershell
python -m pip install -r requirements.txt
python -m desktop
```

Windows 打包：

```powershell
.\desktop\build-windows.ps1
```

macOS 打包：

```sh
sh desktop/build-macos.sh
```

源码运行时默认使用 `data/funds.db`。打包后的 Windows 应用默认使用 `%LOCALAPPDATA%\FundValuation\data\funds.db`，macOS 应用默认使用 `~/Library/Application Support/FundValuation/data/funds.db`。可以通过 `FUNDVAL_DATA_DIR` 指定自定义数据目录。
````

- [ ] **Step 2: Run README sanity check**

Run:

```powershell
Select-String -Path README.md -Pattern "桌面客户端","python -m desktop","build-windows.ps1","FUNDVAL_DATA_DIR"
```

Expected: all four patterns produce matches.

- [ ] **Step 3: Commit**

Run:

```powershell
git add README.md
git commit -m "Document desktop client usage"
```

## Task 8: Full verification and Windows package smoke

**Files:**
- No source files should be modified in this task unless verification exposes a defect.

- [ ] **Step 1: Run full automated verification**

Run:

```powershell
python -m unittest discover -s tests -v
node --check web\app.js
python -m py_compile app.py desktop/runtime.py desktop/server.py desktop/main.py desktop/__main__.py
```

Expected: all commands pass.

- [ ] **Step 2: Build the Windows package**

Run:

```powershell
.\desktop\build-windows.ps1
```

Expected:

- Command exits with code 0.
- `dist\FundValuation\FundValuation.exe` exists.

- [ ] **Step 3: Run packaged Windows smoke test**

Run:

```powershell
.\dist\FundValuation\FundValuation.exe
```

Expected:

- A desktop window titled `Fund Valuation` opens.
- The existing page renders.
- `/api/health` is reachable from the embedded window.
- Closing the window exits the executable.
- Desktop data is stored under `%LOCALAPPDATA%\FundValuation\data\funds.db`, not under a PyInstaller temporary directory.

- [ ] **Step 4: Record macOS verification boundary**

If no macOS machine is available, do not claim macOS release readiness. Record the boundary in the final response:

```text
macOS build script is present but was not smoke-tested on macOS from this Windows environment.
```

- [ ] **Step 5: Final status check**

Run:

```powershell
git status --short
git log --oneline -5
```

Expected:

- `git status --short` is empty, or only ignored build outputs exist outside Git tracking.
- Recent commits correspond to the completed desktop tasks.

## Self-Review

Spec coverage:

- Preserve existing functionality: Tasks 1, 3, 4, and 8 reuse `app.create_app()` and run the existing test suite.
- Local single-machine storage: Tasks 1 and 2 configure `FUNDVAL_DATA_DIR` and packaged local user-data paths.
- Desktop startup flow: Tasks 2, 3, and 4 implement port selection, health polling, window launch, and shutdown.
- Error handling and logs: Task 4 writes `logs/desktop.log` and shows startup failures through a dialog or stderr fallback.
- Windows/macOS packaging: Task 6 adds platform-specific PyInstaller scripts, and Task 8 verifies Windows while explicitly bounding macOS.
- Documentation: Task 7 documents desktop run, packaging, and data locations.

Deferred-marker scan:

- The plan contains no deferred implementation markers.
- Every code-changing task includes exact file paths, code snippets, commands, and expected outcomes.

Type consistency:

- `DATA_DIR_ENV`, `build_local_url`, `find_free_port`, `resolve_app_data_dir`, `resolve_log_file`, and `configure_desktop_environment` are defined in Task 2 before use elsewhere.
- `DesktopServer`, `wait_for_health`, `start`, `wait_until_ready`, `stop`, and `url` are defined in Task 3 before use in Task 4.
- `create_app(data_dir=...)` and `api.state.valuation_service` are added in Task 1 before the test asserts on them.
