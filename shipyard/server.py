"""The server's HTTP side: serves the page, the server's state with your local
checkouts (once as JSON and as a Server-Sent Events stream), the GitHub reads the page asks for
(merged history, a PR's timeline), and the POSTs behind every button.

Binds to localhost. Every request must carry a localhost Host header (against DNS
rebinding), and every mutation must also come from the page's own origin with the
token written into the page it serves."""
import http.server
import json
import re
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import worktrees
from .config import DEFAULT_CONFIG, page_config
from .settings import MISSING

MAX_BODY = 1_000_000
SSE_KEEPALIVE = 15


class Handler(http.server.SimpleHTTPRequestHandler):
    roots = []
    config = DEFAULT_CONFIG
    session_token = ""
    orchestrator = None
    STATIC_PATHS = {"/", "/index.html", "/favicon.svg", "/docs/board-view.png",
                    "/manifest.webmanifest", "/icons/icon-192.png",
                    "/icons/icon-512.png", "/icons/icon-maskable-512.png",
                    "/icons/apple-touch-icon.png"}
    # The page's stylesheet and modules. They change with every pull, so the browser revalidates them.
    PAGE_FILES = re.compile(r"/(app\.css|js/[a-z]+\.js)")
    # Older Pythons don't know this extension; Chrome expects the manifest MIME type.
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".webmanifest": "application/manifest+json",
                      ".js": "text/javascript", ".css": "text/css"}
    BRANCH_ACTIONS = {"/new-task", "/new-branch-main", "/open-in-main",
                      "/open-default-main", "/open-app", "/open-agent"}
    CHECKOUT_ACTIONS = {"/new-task", "/new-branch-main", "/open-in-main", "/open-default-main"}
    API_ACTIONS = {"/api/snooze", "/api/pin", "/api/unpin", "/api/ask", "/api/run-pass",
                         "/api/card-action", "/api/dismiss", "/api/undo",
                         "/api/pr-action", "/api/auto-approve",
                         "/api/check-now", "/api/rescan-worktrees", "/api/settings",
                         "/api/current-view", "/api/automation"}

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

    def _is_static(self, path):
        return path in self.STATIC_PATHS or bool(self.PAGE_FILES.fullmatch(path))

    def end_headers(self):
        if self.PAGE_FILES.fullmatch(urlparse(self.path).path):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

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
        if path in ("/", "/index.html"):
            return self._page()
        if path == "/api/state":
            return self._json(self.orchestrator.page_state())
        if path == "/api/events":
            return self._events()
        if path == "/api/settings":
            return self._api_call(lambda c: c.settings())
        if path == "/api/timeline":
            q = parse_qs(parsed.query)
            return self._api_call(lambda c: c.timeline((q.get("id") or [""])[0]))
        if path == "/api/merged":
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            return self._api_call(lambda c: c.merged_history(q.get("view", ""), q.get("kind", ""), q.get("after")))
        if path == "/api/sessions":
            q = parse_qs(parsed.query)
            return self._api_call(lambda c: c.sessions_for((q.get("key") or [""])[0]))
        if self._is_static(path):
            return super().do_GET()
        return self._not_found()

    def _page(self):
        """index.html with the session token and config written in, so the page can POST
        without first asking for them."""
        boot = json.dumps({**page_config(self.orchestrator.config), "token": self.session_token,
                           "views": self.orchestrator.views()}).replace("<", "\\u003c")
        tag = f'<script id="shipyard-boot" type="application/json">{boot}</script>\n</head>'
        html = Path(self.directory, "index.html").read_text().replace("</head>", tag, 1)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _events(self):
        """Server-Sent Events: the whole page state now, then again on every change, with
        a comment line as keepalive so a dropped connection is noticed."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen = sent = sent_poll = sent_merged = None
        try:
            while True:
                if seen != self.orchestrator.version:
                    seen = self.orchestrator.version
                    # Merged history is large and rarely changes, so it goes out on its own.
                    if sent_merged != self.orchestrator.merged_version:
                        sent_merged = self.orchestrator.merged_version
                        self.wfile.write(f"event: merged\ndata: {json.dumps(self.orchestrator.recent_merged)}\n\n".encode())
                    state = self.orchestrator.page_state()
                    status = state.get("status") or {}
                    # Every poll bumps the version and lastPoll; only the rest is worth a re-render.
                    content = json.dumps({**state, "version": None,
                                          "status": {**status, "lastPoll": None}}, sort_keys=True)
                    if content != sent:
                        sent, sent_poll = content, status.get("lastPoll")
                        self.wfile.write(f"event: state\ndata: {json.dumps(state)}\n\n".encode())
                    elif status.get("lastPoll") != sent_poll:
                        sent_poll = status.get("lastPoll")
                        self.wfile.write(f"event: poll\ndata: {json.dumps({'lastPoll': sent_poll})}\n\n".encode())
                    else:
                        self.wfile.write(b": keepalive\n\n")
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                self.orchestrator.wait_for_change(seen, SSE_KEEPALIVE)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_HEAD(self):
        if not self._host_is_allowed():
            return self.send_error(403, "Invalid Host header")
        if self._is_static(urlparse(self.path).path):
            return super().do_HEAD()
        return self._not_found()

    def _api_call(self, fn):
        try:
            return self._json(fn(self.orchestrator))
        except (LookupError, ValueError) as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # GitHub or git failures: show the reason on the card
            return self._json({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in self.BRANCH_ACTIONS | self.API_ACTIONS:
            return self._not_found()
        if not self._mutation_is_authorized():
            return self._json({"ok": False, "error": "Request was not authorized. Reload the page and try again."}, 403)
        q = parse_qs(parsed.query)
        arg = lambda name: (q.get(name) or [""])[0]  # noqa: E731
        if parsed.path in self.API_ACTIONS:
            try:
                body = self._body()
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._api(parsed.path.rsplit("/", 1)[1], arg, body)
        if parsed.path == "/new-task":
            result = worktrees.new_task_worktree(arg("repo"), arg("branch"), self.roots)
        elif parsed.path == "/new-branch-main":
            result = worktrees.new_branch_in_main(arg("repo"), arg("branch"), self.roots)
        elif parsed.path == "/open-in-main":
            result = worktrees.open_in_main(arg("repo"), arg("branch"), self.roots)
        elif parsed.path == "/open-default-main":
            result = worktrees.open_default_in_main(arg("repo"), self.roots)
        elif parsed.path == "/open-app":
            result = worktrees.launch_app(arg("path"), self.roots, self.config)
        else:
            result = worktrees.launch_agent(arg("path"), self.roots, self.config, arg("session") or None)
        if result.get("ok") and parsed.path in self.CHECKOUT_ACTIONS:
            self.orchestrator.rescan_worktrees()
        return self._json(result, 200 if result.get("ok") else 500)

    def _api(self, name, arg, body):
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
            "check-now": lambda c: c.check_now(bool(body.get("merged"))),
            "rescan-worktrees": lambda c: c.rescan_worktrees(),
            "settings": lambda c: c.save_setting(body.get("path"), body.get("value"), bool(body.get("reset")),
                                                 body["expect"] if "expect" in body else MISSING),
            "current-view": lambda c: c.set_current_view(body.get("view")),
            "automation": lambda c: c.turn_on_automation(),
            "auto-approve": lambda c: c.set_auto_approve(arg("key"), body.get("on")),
            "pr-action": lambda c: c.do_action(arg("key"), arg("action"), body.get("payload") or {},
                                               int(arg("card")) if arg("card") else None),
        }
        return self._api_call(calls[name])

    def log_message(self, *args):
        pass
