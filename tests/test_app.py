import http.server
import threading
import unittest

from shipyard.app import bind, is_shipyard
from test_server import ServerCase


def serve(server_name):
    """A server on a free localhost port whose responses carry this Server header."""
    class Page(http.server.BaseHTTPRequestHandler):
        def version_string(self):
            return server_name

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class PortInUse(unittest.TestCase):
    def port_held_by(self, server_name):
        httpd = serve(server_name)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_port

    def test_a_running_shipyard_is_named_with_its_url(self):
        port = self.port_held_by("Shipyard")
        with self.assertRaises(SystemExit) as caught:
            bind(port)
        self.assertEqual(caught.exception.code, f"Shipyard is already running → http://localhost:{port}")

    def test_another_program_on_the_port_suggests_another_port(self):
        port = self.port_held_by("SimpleHTTP/0.6 Python/3.13")
        with self.assertRaises(SystemExit) as caught:
            bind(port)
        self.assertIn(f"Port {port} is in use by another program", caught.exception.code)
        self.assertIn("--port", caught.exception.code)

    def test_a_free_port_binds(self):
        httpd = bind(0)
        self.addCleanup(httpd.server_close)
        self.assertNotEqual(httpd.server_port, 0)


class RecognizesItself(ServerCase):
    def test_the_real_server_is_recognized_as_shipyard(self):
        self.assertTrue(is_shipyard(self.port))


if __name__ == "__main__":
    unittest.main()
