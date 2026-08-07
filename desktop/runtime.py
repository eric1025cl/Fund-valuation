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
