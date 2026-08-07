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
        force=True,
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
