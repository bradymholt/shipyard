"""The companion's HTTP side: serves the page, /worktrees.json, the companion's state
(once as JSON and as a Server-Sent Events stream), and the POSTs behind every button.

Binds to localhost. Every request must carry a localhost Host header (against DNS
rebinding), and every mutation must also come from the page's own origin with the
per-process token it gets from /config.json."""
import http.server
import json
import secrets
from urllib.parse import parse_qs, urlparse

from . import worktrees
from .config import DEFAULT_CONFIG, page_config

MAX_BODY = 1_000_000
SSE_KEEPALIVE = 15


class Handler(http.server.SimpleHTTPRequestHandler):
    roots = []
    config = DEFAULT_CONFIG
    session_token = ""
    companion = None
    STATIC_PATHS = {"/", "/index.html", "/favicon.svg", "/docs/board-view.png",
                    "/manifest.webmanifest", "/icons/icon-192.png",
                    "/icons/icon-512.png", "/icons/icon-maskable-512.png",
                    "/icons/apple-touch-icon.png"}
    # Older Pythons don't know this extension; Chrome expects the manifest MIME type.
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".webmanifest": "application/manifest+json"}
    BRANCH_ACTIONS = {"/new-task", "/new-branch-main", "/open-in-main", "/move-to-worktree",
                      "/open-default-main", "/open-app", "/open-agent"}
    COMPANION_ACTIONS = {"/companion/snooze", "/companion/pin", "/companion/unpin", "/companion/ask", "/companion/run-pass",
                         "/companion/card-action", "/companion/dismiss", "/companion/undo",
                         "/companion/pr-action",
                         "/companion/check-now"}

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _allowed_hosts(self):
        port = self.server.server_port
        hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
        if port == 80:
            hosts.update({"localhost", "127.0.0.1"})
        return hosts

    def _host_is_allowed(self):
        return self.headers.get("Host", "").lower() in self._allowed_hosts()

    def _mutation_is_authorized(self):
        host = self.headers.get("Host", "").lower()
        if not self._host_is_allowed():
            return False
        if self.headers.get("Origin", "").lower() != f"http://{host}":
            return False
        supplied = self.headers.get("X-Shipyard-Token", "")
        return bool(supplied and secrets.compare_digest(supplied, self.session_token))

    def _not_found(self):
        self.send_error(404)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}
        if length > MAX_BODY:
            raise ValueError("Request body is too large.")
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError as e:
            raise ValueError("Request body must be JSON.") from e
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        return body

    def do_GET(self):
        if not self._host_is_allowed():
            return self._json({"ok": False, "error": "Invalid Host header."}, 403)
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/worktrees.json":
            return self._json(worktrees.discover(self.roots, self.config["agent"]))
        if path == "/config.json":
            cfg = self.companion.config if self.companion else self.config
            return self._json({**page_config(cfg), "companionToken": self.session_token,
                               "orchestrator": bool(self.companion)})
        if path == "/companion/state":
            if not self.companion:
                return self._json({"ok": False, "error": "The companion's orchestrator is off."}, 404)
            return self._json(self.companion.page_state())
        if path == "/companion/events":
            return self._events()
        if path == "/companion/sessions":
            q = parse_qs(parsed.query)
            return self._companion_call(lambda c: c.sessions_for((q.get("key") or [""])[0]))
        if path in self.STATIC_PATHS:
            return super().do_GET()
        return self._not_found()

    def _events(self):
        """Server-Sent Events: the whole page state now, then again on every change, with
        a comment line as keepalive so a dropped connection is noticed."""
        if not self.companion:
            return self._json({"ok": False, "error": "The companion's orchestrator is off."}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen = None
        try:
            while True:
                if seen != self.companion.version:
                    seen = self.companion.version
                    data = json.dumps(self.companion.page_state())
                    self.wfile.write(f"event: state\ndata: {data}\n\n".encode())
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                self.companion.wait_for_change(seen, SSE_KEEPALIVE)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_HEAD(self):
        if not self._host_is_allowed():
            return self.send_error(403, "Invalid Host header")
        if urlparse(self.path).path in self.STATIC_PATHS:
            return super().do_HEAD()
        return self._not_found()

    def _companion_call(self, fn):
        if not self.companion:
            return self._json({"ok": False, "error": "The companion's orchestrator is off."}, 404)
        try:
            return self._json(fn(self.companion))
        except (LookupError, ValueError) as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # GitHub or git failures: show the reason on the card
            return self._json({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in self.BRANCH_ACTIONS | self.COMPANION_ACTIONS:
            return self._not_found()
        if not self._mutation_is_authorized():
            return self._json({"ok": False, "error": "Companion request was not authorized."}, 403)
        q = parse_qs(parsed.query)
        arg = lambda name: (q.get(name) or [""])[0]  # noqa: E731
        if parsed.path in self.COMPANION_ACTIONS:
            try:
                body = self._body()
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._companion(parsed.path.rsplit("/", 1)[1], arg, body)
        if parsed.path == "/new-task":
            result = worktrees.new_task_worktree(arg("repo"), arg("branch"), self.roots)
        elif parsed.path == "/new-branch-main":
            result = worktrees.new_branch_in_main(arg("repo"), arg("branch"), self.roots)
        elif parsed.path == "/open-in-main":
            result = worktrees.open_in_main(arg("repo"), arg("branch"), self.roots)
        elif parsed.path == "/move-to-worktree":
            result = worktrees.move_to_worktree(arg("repo"), arg("branch"), self.roots)
        elif parsed.path == "/open-default-main":
            result = worktrees.open_default_in_main(arg("repo"), self.roots)
        elif parsed.path == "/open-app":
            result = worktrees.launch_app(arg("path"), self.roots, self.config)
        else:
            result = worktrees.launch_agent(arg("path"), self.roots, self.config, arg("session") or None)
        return self._json(result, 200 if result.get("ok") else 500)

    def _companion(self, name, arg, body):
        def card_id():
            try:
                return int(arg("card"))
            except ValueError as e:
                raise ValueError("Missing card id.") from e

        calls = {
            "snooze": lambda c: c.snooze(arg("key"), body.get("hours")),
            "pin": lambda c: c.repin(arg("key"), arg("session")),
            "unpin": lambda c: c.unpin(arg("key")),
            "ask": lambda c: c.ask(arg("key"), body.get("question")),
            "run-pass": lambda c: c.run_pass_now(arg("key"), arg("pass"), arg("node") or None),
            "card-action": lambda c: c.card_action(card_id(), int(arg("index") or 0), body.get("payload")),
            "dismiss": lambda c: c.dismiss(card_id()),
            "undo": lambda c: c.undo(card_id()),
            "check-now": lambda c: c.check_now(),
            "pr-action": lambda c: c.do_action(arg("key"), arg("action"), body.get("payload") or {},
                                               int(arg("card")) if arg("card") else None),
        }
        return self._companion_call(calls[name])

    def log_message(self, *args):
        pass
