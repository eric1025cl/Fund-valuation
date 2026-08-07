import logging
import tempfile
import unittest
from pathlib import Path

from desktop.main import configure_logging


class DesktopMainTests(unittest.TestCase):
    def test_configure_logging_writes_to_desktop_log_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "desktop.log"

            configure_logging(log_file)
            logging.getLogger("desktop-test").info("hello desktop")
            logging.shutdown()

            self.assertIn("hello desktop", log_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
