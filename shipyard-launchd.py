#!/usr/bin/env python3
"""Run the Shipyard companion at login, as a macOS launchd user agent.

macOS only: launchd is the macOS service manager and has no equivalent
elsewhere. Everything else in Shipyard is cross-platform.

    ./shipyard-launchd.py install     write the plists and start the agents
    ./shipyard-launchd.py status      state, pid, restart count, log sizes
    ./shipyard-launchd.py restart     stop and start the companion
    ./shipyard-launchd.py logs [-f]   show or follow the companion's output
    ./shipyard-launchd.py uninstall   stop the agents and remove the plists

Two agents are involved: `local.shipyard` runs the companion, and
`local.shipyard-restart` watches the companion's files and reloads it on save.

`install` is idempotent, so re-run it after moving the repo, upgrading Python,
or creating `shipyard.config.json`. A plist that is a symlink is left alone and
only loaded, so an external manager such as a dotfiles repo stays the owner.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

LABEL = "local.shipyard"
WATCH_LABEL = "local.shipyard-restart"
REPO = Path(__file__).resolve().parent
COMPANION = REPO / "shipyard.py"
CONFIG = REPO / "shipyard.config.json"
AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST = AGENT_DIR / f"{LABEL}.plist"
WATCH_PLIST = AGENT_DIR / f"{WATCH_LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs"
OUT_LOG = LOG_DIR / "shipyard.out.log"
ERR_LOG = LOG_DIR / "shipyard.err.log"
BASE_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
SETTLE_SECONDS = 3.0
START_TIMEOUT = 10.0
POLL_SECONDS = 0.25


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def domain() -> str:
    return f"gui/{os.getuid()}"


def target(label: str) -> str:
    return f"{domain()}/{label}"


def require_macos() -> None:
    if sys.platform != "darwin":
        die(f"launchd agents are macOS only (this is {sys.platform}); "
            "run `python3 shipyard.py` directly instead")


def choose_python(explicit: str | None) -> str:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute() or not path.exists():
            die(f"--python needs an existing absolute path, got: {explicit}")
        return str(path)
    # Prefer the PATH symlink (/opt/homebrew/bin/python3) over sys.executable,
    # which can point inside a version-pinned Cellar directory that stops
    # existing at the next Python upgrade.
    return shutil.which("python3") or sys.executable


def agent_path(python: str) -> str:
    # The companion shells out to git, and launchd agents inherit almost no PATH.
    home = str(Path(python).parent)
    return ":".join([home] + [p for p in BASE_PATH if p != home])


def service_plist(python: str) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [python, str(COMPANION)],
        "WorkingDirectory": str(REPO),
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {"PATH": agent_path(python)},
        "StandardOutPath": str(OUT_LOG),
        "StandardErrorPath": str(ERR_LOG),
    }


def watch_plist() -> dict:
    # A missing WatchPaths entry can make launchd fire the job repeatedly, so
    # only watch files that exist; re-run install after adding the config.
    watched = [str(p) for p in (COMPANION, CONFIG) if p.exists()]
    return {
        "Label": WATCH_LABEL,
        "ProgramArguments": ["/bin/sh", "-c", f"launchctl kickstart -k {target(LABEL)}"],
        "WatchPaths": watched,
        "RunAtLoad": False,
        "StandardOutPath": str(LOG_DIR / "shipyard-restart.out.log"),
        "StandardErrorPath": str(LOG_DIR / "shipyard-restart.err.log"),
    }


def read_plist(path: Path) -> dict | None:
    try:
        with path.open("rb") as handle:
            return plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return None


def configured_logs(path: Path) -> tuple[Path, Path]:
    """Log paths from the installed plist, which may not be one we wrote."""
    data = read_plist(path) or {}
    out = Path(str(data.get("StandardOutPath", OUT_LOG))).expanduser()
    err = Path(str(data.get("StandardErrorPath", ERR_LOG))).expanduser()
    return out, err


def snapshot(label: str) -> dict[str, str] | None:
    """Parse `launchctl print`, or None when the agent isn't loaded."""
    proc = run(["launchctl", "print", target(label)])
    if proc.returncode != 0:
        return None
    wanted = ("state", "pid", "runs", "last exit code")
    fields: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        # `launchctl print` repeats `state` for nested endpoints; the first
        # occurrence is the service's own.
        if sep and key in wanted and key not in fields:
            fields[key] = value.strip()
    return fields


def as_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def tail(path: Path, lines: int = 20) -> str:
    if not path.exists():
        return ""
    return run(["tail", "-n", str(lines), str(path)]).stdout.rstrip()


def show_failure_logs() -> None:
    out_log, err_log = configured_logs(PLIST)
    for name, path in (("stderr", err_log), ("stdout", out_log)):
        text = tail(path)
        if text:
            print(f"\n--- last lines of {name} ({path}) ---")
            print(text)


def report_stopped(fields: dict[str, str] | None, respawning: bool = False) -> int:
    exit_code = ((fields or {}).get("last exit code") or "").strip()
    # KeepAlive restarts a crashing process, so a climbing run count exposes a
    # fast crash loop, while a nonzero exit code catches a slow one: launchd
    # throttles respawns to roughly ten seconds and parks the job in
    # `spawn scheduled` in between.
    if respawning or (exit_code.isdigit() and exit_code != "0"):
        print(f"crash loop: the companion exited with code {exit_code or '?'} "
              "and launchd keeps respawning it")
    else:
        print(f"not running: state = {(fields or {}).get('state', 'unknown')}, "
              f"last exit code = {exit_code or 'unknown'}")
    show_failure_logs()
    return 1


def wait_for_running() -> dict[str, str] | None:
    """Poll until the companion reports running, or the budget runs out.

    launchd reports `spawn scheduled` for a moment after bootstrap, so a single
    sample right after loading can miss a service that is starting normally.
    """
    deadline = time.monotonic() + START_TIMEOUT
    fields = snapshot(LABEL)
    while fields is not None and fields.get("state") != "running":
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
        fields = snapshot(LABEL)
    return fields


def verify_service() -> int:
    """Confirm the companion is up and not respawning. Returns an exit code."""
    before = wait_for_running()
    if before is None:
        print("not running: the agent did not load")
        return 1
    if before.get("state") != "running":
        return report_stopped(before)

    time.sleep(SETTLE_SECONDS)
    after = snapshot(LABEL)
    if after is None:
        print("not running: the agent disappeared after starting")
        show_failure_logs()
        return 1

    respawning = as_int(after.get("runs")) > as_int(before.get("runs"))
    if after.get("state") == "running" and not respawning:
        print(f"companion    running (pid {after.get('pid', '?')})")
        return 0
    return report_stopped(after, respawning)


def external_owner(path: Path) -> Path | None:
    """The real file behind a symlinked plist, when something else manages it."""
    return path.resolve() if path.is_symlink() else None


def ensure_plist(path: Path, data: dict) -> None:
    owner = external_owner(path)
    if owner:
        print(f"             managed elsewhere, left unchanged: {owner}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(data, handle)


def load(label: str, path: Path) -> None:
    if not path.exists():
        die(f"no plist at {path}")
    # launchd caches the loaded definition, so an edited plist only takes effect
    # after a bootout/bootstrap cycle.
    run(["launchctl", "bootout", target(label)])
    proc = run(["launchctl", "bootstrap", domain(), str(path)])
    if proc.returncode != 0:
        die(f"launchctl bootstrap failed for {label}: "
            f"{(proc.stderr or proc.stdout).strip()}")


def warn_on_foreign_plist() -> None:
    """An externally managed plist may not point at this checkout."""
    if not external_owner(PLIST):
        return
    data = read_plist(PLIST)
    if data is None:
        print(f"warning: {PLIST} could not be parsed as a plist")
    elif str(COMPANION) not in (data.get("ProgramArguments") or []):
        print(f"warning: {PLIST} does not run {COMPANION}; "
              "edit it where it is managed, or remove it to let this script own it")


def cmd_install(args: argparse.Namespace) -> int:
    if not COMPANION.exists():
        die(f"cannot find the companion at {COMPANION}")

    python = choose_python(args.python)
    if run([python, "-c", ""]).returncode != 0:
        die(f"{python} is not a working Python interpreter (try --python)")

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print(f"interpreter  {python}")
    print(f"companion    {COMPANION}")
    print(f"service      {PLIST}")
    ensure_plist(PLIST, service_plist(python))
    print(f"watcher      {WATCH_PLIST}")
    ensure_plist(WATCH_PLIST, watch_plist())
    if not CONFIG.exists():
        print(f"             {CONFIG.name} not found, so it is not watched yet")

    warn_on_foreign_plist()
    out_log, err_log = configured_logs(PLIST)
    print(f"logs         {out_log}\n             {err_log}")

    load(LABEL, PLIST)
    # Verify before loading the watcher: a WatchPaths job can fire as it loads,
    # and that kickstart would look like the companion respawning on its own.
    code = verify_service()

    load(WATCH_LABEL, WATCH_PLIST)
    watcher = snapshot(WATCH_LABEL)
    print(f"watcher      {'loaded' if watcher else 'NOT loaded'}"
          f"{'' if watcher else ' (reload on save is inactive)'}")
    return code


def describe(label: str, path: Path, on_demand: bool = False) -> dict[str, str] | None:
    """Print one agent's state. Returns its fields, or None when not loaded."""
    owner = external_owner(path)
    where = f"{path} -> {owner}" if owner else str(path)
    print(f"plist        {where}{'' if path.exists() else '  (missing)'}")

    fields = snapshot(label)
    if fields is None:
        print("state        not loaded")
        return None
    state = fields.get("state", "unknown")
    # A WatchPaths job sits idle until a file changes, so anything other than
    # "not loaded" is healthy for it.
    if on_demand and state != "running":
        print("state        loaded, waiting for changes")
    else:
        print(f"state        {state}")
    if fields.get("pid"):
        print(f"pid          {fields['pid']}")
    print(f"restarts     {fields.get('runs', '-')}")
    print(f"last exit    {fields.get('last exit code', '-')}")
    return fields


def cmd_status(args: argparse.Namespace) -> int:
    print(f"== {LABEL} (companion) ==")
    service = describe(LABEL, PLIST)
    for name, path in zip(("stdout", "stderr"), configured_logs(PLIST)):
        size = f"{path.stat().st_size} bytes" if path.exists() else "missing"
        print(f"{name:<12} {path} ({size})")

    print(f"\n== {WATCH_LABEL} (reload on save) ==")
    watcher = describe(WATCH_LABEL, WATCH_PLIST, on_demand=True)

    if service is None or watcher is None:
        print("\nAn agent is not loaded. Writing the plist is not enough, since "
              "launchd only\npicks it up at login or when told to:\n"
              "  ./shipyard-launchd.py install")
        return 1
    if service.get("state") != "running":
        print("\nThe companion is loaded but not running. Check what it printed "
              "on the way down:\n  ./shipyard-launchd.py logs")
        return 1
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    if snapshot(LABEL) is None:
        die("the agent is not loaded; run `./shipyard-launchd.py install` first")
    proc = run(["launchctl", "kickstart", "-k", target(LABEL)])
    if proc.returncode != 0:
        die(f"launchctl kickstart failed: {(proc.stderr or proc.stdout).strip()}")
    return verify_service()


def cmd_logs(args: argparse.Namespace) -> int:
    existing = [p for p in configured_logs(PLIST) if p.exists()]
    if not existing:
        print("no logs yet at " + " or ".join(str(p) for p in configured_logs(PLIST)))
        return 1

    if args.follow:
        try:
            subprocess.run(["tail", "-f", *[str(p) for p in existing]])
        except KeyboardInterrupt:
            pass
        return 0

    for path in existing:
        print(f"--- {path} ---")
        print(tail(path, args.lines) or "(empty)")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    for label, path in ((WATCH_LABEL, WATCH_PLIST), (LABEL, PLIST)):
        run(["launchctl", "bootout", target(label)])
        owner = external_owner(path)
        if owner:
            print(f"{label}: stopped; plist left in place, managed at {owner}")
        elif path.exists():
            path.unlink()
            print(f"{label}: stopped and removed {path}")
        else:
            print(f"{label}: stopped; no plist at {path}")
    print("logs were left in place")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage the Shipyard companion as a macOS launchd agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="write the plists and start the agents")
    install.add_argument("--python", metavar="PATH",
                         help="absolute path to the interpreter to run the companion")
    install.set_defaults(func=cmd_install)

    sub.add_parser("status", help="show state, pid, restart count, and logs") \
        .set_defaults(func=cmd_status)
    sub.add_parser("restart", help="stop and start the companion") \
        .set_defaults(func=cmd_restart)

    logs = sub.add_parser("logs", help="show the companion's output")
    logs.add_argument("-f", "--follow", action="store_true", help="keep watching")
    logs.add_argument("-n", "--lines", type=int, default=20, help="lines to show")
    logs.set_defaults(func=cmd_logs)

    sub.add_parser("uninstall", help="stop the agents and remove the plists") \
        .set_defaults(func=cmd_uninstall)

    args = parser.parse_args()
    require_macos()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
