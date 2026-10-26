import functools
import http.client
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from shipyard.config import validate_config
from shipyard.server import Handler


class FakeCompanion:
    def __init__(self):
        self.version = 1
        self.config = validate_config({})
        self.calls = []
        self.changed = threading.Event()
        self.prs = {"o/r#1": {"chip": None}}
        self.status = {"lastPoll": "t1"}

    def page_state(self):
        return {"version": self.version, "status": dict(self.status), "prs": self.prs}

    def wait_for_change(self, seen, timeout):
        self.changed.wait(timeout)
        self.changed.clear()
        return self.version

    def snooze(self, key, hours):
        self.calls.append(("snooze", key, hours))
        return {"ok": True}

    def card_action(self, card, index, payload):
        self.calls.append(("card", card, index, payload))
        return {"ok": True}

    def do_action(self, key, action, payload, card_id=None):
        raise LookupError("not tracked")


class ServerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Path(tmp.name, "index.html").write_text("<!doctype html>")
        self.companion = FakeCompanion()
        patches = [mock.patch.object(Handler, "companion", self.companion),
                   mock.patch.object(Handler, "session_token", "tok"),
                   mock.patch.object(Handler, "roots", [tmp.name]),
                   mock.patch("shipyard.server.worktrees.discover", return_value=[])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(Handler, directory=tmp.name))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        host = host or f"localhost:{self.port}"
        base = {"Host": host}
        if method == "POST":
            base.update({"Origin": f"http://{host}", "X-Shipyard-Token": "tok",
                         "Content-Type": "application/json"})
        base.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, base)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, (json.loads(data) if data and res.getheader("Content-Type") == "application/json" else data)


class Endpoints(ServerCase):
    def test_config_carries_rules_and_token(self):
        status, cfg = self.request("GET", "/config.json")
        self.assertEqual(status, 200)
        self.assertEqual(cfg["companionToken"], "tok")
        self.assertTrue(cfg["orchestrator"])
        self.assertEqual(cfg["rules"]["nudge"], {"after_hours": 24})

    def test_state_and_host_check(self):
        status, state = self.request("GET", "/companion/state")
        self.assertEqual((status, state["version"]), (200, 1))
        status, _ = self.request("GET", "/companion/state", host="evil.example:80")
        self.assertEqual(status, 403)

    def test_mutations_need_origin_and_token(self):
        status, _ = self.request("POST", "/companion/snooze?key=o/r%231", {"hours": 4},
                                 headers={"X-Shipyard-Token": "wrong"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/companion/snooze?key=o/r%231", {"hours": 4},
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, out = self.request("POST", "/companion/snooze?key=o/r%231", {"hours": 4})
        self.assertEqual((status, out), (200, {"ok": True}))
        self.assertEqual(self.companion.calls, [("snooze", "o/r#1", 4)])

    def test_card_action_body_and_errors(self):
        status, _ = self.request("POST", "/companion/card-action?card=3&index=1", {"payload": {"body": "x"}})
        self.assertEqual(status, 200)
        self.assertEqual(self.companion.calls[-1], ("card", 3, 1, {"body": "x"}))
        status, out = self.request("POST", "/companion/pr-action?key=o/r%231&action=merge", {})
        self.assertEqual((status, out["error"]), (400, "not tracked"))
        status, out = self.request("POST", "/companion/card-action?card=x", {})
        self.assertEqual(status, 400)

    def test_events_stream_sends_state_only_when_it_changed(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/companion/events", headers={"Host": f"localhost:{self.port}"})
        res = conn.getresponse()
        self.assertEqual(res.getheader("Content-Type"), "text/event-stream")

        def read_event():
            lines = []
            while True:
                line = res.fp.readline().decode()
                if line.startswith(": keepalive"):
                    return [line.strip()]
                if line == "\n":
                    if lines and lines[0].startswith("event:"):
                        return lines
                    lines = []
                    continue
                lines.append(line.strip())

        first = read_event()
        self.assertEqual(first[0], "event: state")
        self.assertEqual(json.loads(first[1][len("data: "):])["version"], 1)

        def bump(**changes):
            for k, v in changes.items():
                setattr(self.companion, k, v)
            self.companion.version += 1
            self.companion.changed.set()
            return read_event()

        self.assertEqual(bump()[0], ": keepalive")
        poll = bump(status={"lastPoll": "t2"})
        self.assertEqual((poll[0], json.loads(poll[1][len("data: "):])), ("event: poll", {"lastPoll": "t2"}))
        second = bump(prs={"o/r#1": {"chip": {"kind": "nudge"}}})
        self.assertEqual(second[0], "event: state")
        self.assertEqual(json.loads(second[1][len("data: "):])["version"], 4)
        conn.close()

    def test_orchestrator_off(self):
        with mock.patch.object(Handler, "companion", None):
            status, _ = self.request("GET", "/companion/state")
            self.assertEqual(status, 404)
            status, cfg = self.request("GET", "/config.json")
            self.assertFalse(cfg["orchestrator"])

    def test_github_graphql_is_proxied_with_rate_limit_headers(self):
        sent = []

        class FakeGitHub:
            def forward_graphql(self, body):
                sent.append(body)
                return 403, {"Retry-After": "120"}, b'{"message": "secondary rate limit"}'

        with mock.patch.object(Handler, "github", FakeGitHub()):
            status, _ = self.request("POST", "/github/graphql", {"query": "{ viewer { login } }"},
                                     headers={"X-Shipyard-Token": "wrong"})
            self.assertEqual((status, sent), (403, []))
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            host = f"localhost:{self.port}"
            conn.request("POST", "/github/graphql", json.dumps({"query": "{ viewer { login } }"}),
                         {"Host": host, "Origin": f"http://{host}", "X-Shipyard-Token": "tok"})
            res = conn.getresponse()
            self.assertEqual((res.status, res.getheader("Retry-After")), (403, "120"))
            self.assertEqual(json.loads(res.read()), {"message": "secondary rate limit"})
            conn.close()
        self.assertEqual(sent, [{"query": "{ viewer { login } }", "variables": {}}])

    def test_static_and_unknown_paths(self):
        status, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/shipyard.db")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
