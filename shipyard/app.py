import argparse
import functools
import http.server
import sys

from .companion import Companion
from .config import CONFIG_FILE, REPO_DIR, load_config
from .github import GitHub
from .server import Handler
from .worktrees import normalize_roots, warn_unavailable_roots

DEFAULT_PORT = 4321
DB_FILE = "shipyard.db"


def parse_args(argv):
    def port_number(value):
        try:
            port = int(value)
        except ValueError as e:
            raise argparse.ArgumentTypeError("must be an integer") from e
        if not 1 <= port <= 65535:
            raise argparse.ArgumentTypeError("must be between 1 and 65535")
        return port

    parser = argparse.ArgumentParser(
        description="Serve Shipyard and discover Git worktrees under one or more folders.")
    parser.add_argument("roots", nargs="*", metavar="ROOT",
                        help=f"folder containing Git clones (overrides {CONFIG_FILE})")
    parser.add_argument("-p", "--port", type=port_number, default=DEFAULT_PORT,
                        help=f"localhost port (default: {DEFAULT_PORT})")
    args = parser.parse_args(argv)
    return args.port, args.roots


def main(argv=None):
    port, cli_roots = parse_args(sys.argv[1:] if argv is None else argv)
    config = load_config()
    # Roots come from the command line if given, otherwise the "roots" config key.
    cfg_roots = config.get("roots") or []
    roots = normalize_roots(cli_roots or cfg_roots)
    if not roots:
        sys.exit(f'No folders to scan. Set "roots" in {CONFIG_FILE} (e.g. ["~/dev"]) '
                 f'or pass them on the command line: python3 shipyard.py ~/dev')
    warn_unavailable_roots(roots)
    Handler.roots = roots
    Handler.config = config
    Handler.companion = Companion(config, roots, REPO_DIR / DB_FILE, github=GitHub())
    Handler.companion.move_old_views()
    Handler.session_token = Handler.companion.db.session_token()
    # Threaded: the Server-Sent Events stream holds a connection open per tab.
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                            functools.partial(Handler, directory=str(REPO_DIR)))
    httpd.daemon_threads = True
    print(f"Shipyard → http://localhost:{port}")
    print(f"Scanning worktrees under: {', '.join(roots)}")
    launcher = config["launcher"]
    print(f"Opens in: {launcher['name']} via {launcher['mode']}")
    agent = config["agent"]
    if agent:
        print(f"Resumes sessions in: {agent['name']} via {agent['mode']}")
    print("Watching your PRs on GitHub (token from `gh auth token`); state in " + DB_FILE)
    Handler.companion.start()
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.companion.stop()
