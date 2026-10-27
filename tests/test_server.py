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


class FakeOrchestrator:
    def __init__(self):
        self.version = 1
        self.config = validate_config({})
        self.calls = []
        self.changed = threading.Event()
        self.prs = {"o/r#1": {"chip": None}}
        self.status = {"lastPoll": "t1"}
        self.recent_merged = {"mine": [{"number": 9}], "review": []}
        self.merged_version = 1

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

    def views(self):
        return {"views": ["o/r"], "current": "o/r"}

    def rescan_worktrees(self):
        self.calls.append(("rescan",))
        return {"ok": True}

    def timeline(self, node_id):
        return {"ok": True, "node": {"id": node_id}}

    def do_action(self, key, action, payload, card_id=None):
        raise LookupError("not tracked")


class ServerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Path(tmp.name, "index.html").write_text("<!doctype html><head></head>")
        Path(tmp.name, "js").mkdir()
        Path(tmp.name, "js", "main.js").write_text("")
        self.orchestrator = FakeOrchestrator()
        patches = [mock.patch.object(Handler, "orchestrator", self.orchestrator),
                   mock.patch.object(Handler, "session_token", "tok"),
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
    def test_page_carries_token_and_rules(self):
        status, html = self.request("GET", "/")
        boot = html.decode().split('<script id="shipyard-boot" type="application/json">')[1].split("</script>")[0]
        self.assertEqual((status, json.loads(boot)["token"]), (200, "tok"))
        self.assertEqual(json.loads(boot)["rules"]["nudge"], {"after_hours": 24})
        self.assertEqual(json.loads(boot)["views"]["current"], "o/r")
        status, _ = self.request("GET", "/config.json")
        self.assertEqual(status, 404)

    def test_state_and_host_check(self):
        status, state = self.request("GET", "/api/state")
        self.assertEqual((status, state["version"]), (200, 1))
        status, _ = self.request("GET", "/api/state", host="evil.example:80")
        self.assertEqual(status, 403)

    def test_mutations_need_origin_and_token(self):
        status, _ = self.request("POST", "/api/snooze?key=o/r%231", {"hours": 4},
                                 headers={"X-Shipyard-Token": "wrong"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/snooze?key=o/r%231", {"hours": 4},
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, out = self.request("POST", "/api/snooze?key=o/r%231", {"hours": 4})
        self.assertEqual((status, out), (200, {"ok": True}))
        self.assertEqual(self.orchestrator.calls, [("snooze", "o/r#1", 4)])

    def test_card_action_body_and_errors(self):
        status, _ = self.request("POST", "/api/card-action?card=3&index=1", {"payload": {"body": "x"}})
        self.assertEqual(status, 200)
        self.assertEqual(self.orchestrator.calls[-1], ("card", 3, 1, {"body": "x"}))
        status, out = self.request("POST", "/api/pr-action?key=o/r%231&action=merge", {})
        self.assertEqual((status, out["error"]), (400, "not tracked"))
        status, out = self.request("POST", "/api/card-action?card=x", {})
        self.assertEqual(status, 400)

    def test_events_stream_sends_state_only_when_it_changed(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events", headers={"Host": f"localhost:{self.port}"})
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

        merged = read_event()
        self.assertEqual((merged[0], json.loads(merged[1][len("data: "):])["mine"]), ("event: merged", [{"number": 9}]))
        first = read_event()
        self.assertEqual(first[0], "event: state")
        self.assertEqual(json.loads(first[1][len("data: "):])["version"], 1)

        def bump(**changes):
            for k, v in changes.items():
                setattr(self.orchestrator, k, v)
            self.orchestrator.version += 1
            self.orchestrator.changed.set()
            return read_event()

        self.assertEqual(bump()[0], ": keepalive")
        poll = bump(status={"lastPoll": "t2"})
        self.assertEqual((poll[0], json.loads(poll[1][len("data: "):])), ("event: poll", {"lastPoll": "t2"}))
        second = bump(prs={"o/r#1": {"chip": {"kind": "nudge"}}})
        self.assertEqual(second[0], "event: state")
        self.assertEqual(json.loads(second[1][len("data: "):])["version"], 4)
        self.assertEqual(bump(merged_version=2)[0], "event: merged")
        conn.close()

    def test_rescans_worktrees_on_request_and_has_no_worktrees_json(self):
        status, _ = self.request("POST", "/api/rescan-worktrees", {})
        self.assertEqual((status, self.orchestrator.calls), (200, [("rescan",)]))
        status, _ = self.request("GET", "/worktrees.json")
        self.assertEqual(status, 404)

    def test_timeline_and_no_graphql_proxy(self):
        status, out = self.request("GET", "/api/timeline?id=PR_1")
        self.assertEqual((status, out["node"]), (200, {"id": "PR_1"}))
        status, _ = self.request("POST", "/github/graphql", {"query": "{ viewer { login } }"})
        self.assertEqual(status, 404)

    def test_static_and_unknown_paths(self):
        status, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/shipyard.db")
        self.assertEqual(status, 404)
        status, _ = self.request("GET", "/js/../shipyard.db")
        self.assertEqual(status, 404)

    def test_page_modules_are_revalidated(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/js/main.js", headers={"Host": f"localhost:{self.port}"})
        res = conn.getresponse()
        res.read()
        conn.close()
        self.assertEqual((res.status, res.getheader("Content-Type"), res.getheader("Cache-Control")),
                         (200, "text/javascript", "no-cache"))


if __name__ == "__main__":
    unittest.main()
