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
