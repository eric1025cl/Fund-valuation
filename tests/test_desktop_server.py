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
            httpd.server_close()

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
