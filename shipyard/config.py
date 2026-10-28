"""shipyard.config.json: folders to scan, the app launcher, the coding agent, and the
rule and trust settings that decide what the server does on its own.

    {
      "roots": ["~/dev"],      // folders to scan for git clones (required,
                               // unless roots are passed on the command line)
      "launcher": {
        "name": "VS Code",
        "mode": "url",
        "target": "workspace",
        "url": "vscode://file/{path}",
        "command": ["code", "{path}"]
      },
      "agent": {               // resume a coding agent's latest session in a
        "name": "Claude Code", // checkout; set to null to turn it off
        "sessions": "claude-code",   // or "codex"
        "mode": "url",
        "url": "claude://resume?session={session}",
        "command": ["open", "claude://resume?session={session}"],
        "prompt_url": "claude://code/new?folder={path}&q={prompt}"
      },                       // a new session with {prompt} typed in; null for none
                               // (each "sessions" brings its own defaults for the rest)
      "rules": { "address_feedback": { "prompt": "/pr-review-respond {url} (branch {branch})" },
                 "nudge": { "after_hours": 24 }, "passes": { "ci_failed": "ask" } },
      "auto": { "rerun_checks": { "when": "confidence >= high", "max_per_pr": 2 } },
      "views": ["acme", "me/tools, acme/web"]   // the page's views: repos, orgs, or
    }                                           // users; the settings page edits these
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

from .agents import ADAPTERS

REPO_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = "shipyard.config.json"
CONFIG_PATH = REPO_DIR / CONFIG_FILE

# Each app brings its own defaults; a setting in the file wins over them. The command
# needs that app's command-line tool on the PATH.
LAUNCHER_DEFAULTS = {
    "vscode": {"name": "VS Code", "mode": "url", "target": "workspace",
               "url": "vscode://file/{path}", "command": ["code", "{path}"]},
    "cursor": {"name": "Cursor", "mode": "url", "target": "workspace",
               "url": "cursor://file/{path}", "command": ["cursor", "{path}"]},
    "windsurf": {"name": "Windsurf", "mode": "url", "target": "workspace",
                 "url": "windsurf://file/{path}", "command": ["windsurf", "{path}"]},
    "zed": {"name": "Zed", "mode": "url", "target": "folder",
            "url": "zed://file/{path}", "command": ["zed", "{path}"]},
}
DEFAULT_LAUNCHER = {"app": "vscode", **LAUNCHER_DEFAULTS["vscode"]}
DEFAULT_AGENT = {
    "name": "Claude Code",
    "sessions": "claude-code",
    "mode": "url",
    "url": "claude://resume?session={session}",
    "command": ["open", "claude://resume?session={session}"],
    "prompt_url": "claude://code/new?folder={path}&q={prompt}",
}
# Codex has no resume URL, so its default opens Terminal in the checkout and runs
# `codex resume`. The path and session go in as arguments, never into the script text,
# and `quoted form of` makes them safe for the shell.
CODEX_RESUME = ["osascript",
                "-e", "on run argv",
                "-e", 'tell application "Terminal" to do script "cd " & quoted form of item 1 of argv'
                      ' & " && codex resume " & quoted form of item 2 of argv',
                "-e", 'tell application "Terminal" to activate',
                "-e", "end run",
                "{path}", "{session}"]
# Each kind of session brings its own defaults; a setting in the file wins over them.
AGENT_DEFAULTS = {
    "claude-code": DEFAULT_AGENT,
    "codex": {"name": "Codex", "sessions": "codex", "mode": "command", "url": "",
              "command": CODEX_RESUME, "prompt_url": None},
}

# What each rule does is code in rules.py; whether and when it fires is config. A rule
# set to false shows no chip and runs no pass. Thresholds take their window here.
RULE_DEFAULTS = {
    "address_feedback": {"prompt": "Address the review feedback on {url} (branch {branch})"},
    "resolve_conflicts": {"prompt": "Merge {base} into {branch} and resolve the conflicts on {url}"},
    "update_stack": True,
    "fix_ci": True,
    "merge": True,
    "review": True,
    "re_review": True,
    "nudge": {"after_hours": 24},
    "ask_rereview": {"after_hours": 24},
    "rerequest_review": True,
    "image_sign_off": True,
    "queue_rerun": True,
    "passes": {
        "feedback": False,
        "ci_failed": "auto",
        "pre_review": "auto",
        "re_review": "auto",
    },
    "instructions": {},  # per pass, added to its prompt
}
RULE_THRESHOLDS = {"nudge": "after_hours", "ask_rereview": "after_hours"}
RULE_PROMPTS = {"address_feedback", "resolve_conflicts"}
PASS_MODES = ("auto", "ask", False)

# Trust flags: every action type but re-review approval and re-running flaky checks starts manual. "when" sets the confidence an
# agent's proposal needs before the server presses the button itself.
AUTO_DEFAULTS = {
    "rerun_checks": True,
    "approve_rereview": True,
    "post_reply": False,
    "apply_fix": False,
}
# These put words in front of a colleague under your name; they never fire on their own.
HARD_LOCKED = {"post_reply"}
# No pass ever proposed it; older files that set it still load.
RETIRED_AUTO = {"enable_auto_merge"}
CONFIDENCE = {"low": 1, "medium": 2, "high": 3}

APPROVE_REPLIES = ["LGTM", "Looks great!", "\u2764\ufe0f", "\U0001faf6"]

DEFAULT_CONFIG = {"roots": [], "launcher": DEFAULT_LAUNCHER, "agent": DEFAULT_AGENT,
                  "rules": RULE_DEFAULTS, "auto": AUTO_DEFAULTS, "views": [], "approve_replies": APPROVE_REPLIES}
TARGET = re.compile(r"[A-Za-z0-9-]+(/[A-Za-z0-9._-]+)?")


def view_targets(view):
    targets = [t.strip() for t in (view or "").split(",") if t.strip()] if isinstance(view, str) else []
    if not targets:
        raise ValueError("No repo, org, or user to search.")
    for t in targets:
        if not TARGET.fullmatch(t):
            raise ValueError(f'"{t}" is not a repo, org, or user.')
    return targets


def validate_views(raw):
    if not isinstance(raw, list):
        raise ValueError('"views" must be an array')
    try:
        return list(dict.fromkeys(", ".join(view_targets(v)) for v in raw))
    except ValueError as e:
        raise ValueError(f'"views": {e}') from e


def validate_open_settings(label, settings, placeholder):
    if not isinstance(settings["name"], str) or not settings["name"].strip():
        raise ValueError(f'{label} "name" must be a non-empty string')
    if settings["mode"] not in ("url", "command"):
        raise ValueError(f'{label} "mode" must be either "url" or "command"')
    if not isinstance(settings["url"], str):
        raise ValueError(f'{label} "url" must be a string')
    if (not isinstance(settings["command"], list) or not settings["command"] or
            any(not isinstance(arg, str) for arg in settings["command"])):
        raise ValueError(f'{label} "command" must be a non-empty array of strings')
    if settings["mode"] == "url":
        has_placeholder = placeholder in settings["url"]
        scheme = urlparse(settings["url"].replace(placeholder, "x")).scheme.lower()
        if not scheme or scheme in ("data", "javascript"):
            raise ValueError(f'{label} "url" must use a safe URL scheme')
    else:
        has_placeholder = any(placeholder in arg for arg in settings["command"])
    if not has_placeholder:
        raise ValueError(f'{label} {settings["mode"]!r} must include a "{placeholder}" placeholder')


def validate_agent(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError('"agent" must be an object or null')
    unknown = sorted(set(raw) - set(DEFAULT_AGENT))
    if unknown:
        raise ValueError(f"unknown agent setting(s): {', '.join(unknown)}")
    sessions = raw.get("sessions", DEFAULT_AGENT["sessions"])
    if sessions not in ADAPTERS:
        raise ValueError(f'agent "sessions" must be one of: {", ".join(ADAPTERS)}')
    agent = {**AGENT_DEFAULTS[sessions], **raw}
    validate_open_settings("agent", agent, "{session}")
    url = agent["prompt_url"]
    if url is not None and (not isinstance(url, str) or "{prompt}" not in url or
                            urlparse(url).scheme.lower() in ("", "data", "javascript")):
        raise ValueError('agent "prompt_url" must be null or a safe URL with a "{prompt}" placeholder')
    return agent


def validate_rules(raw):
    if not isinstance(raw, dict):
        raise ValueError('"rules" must be an object')
    unknown = sorted(set(raw) - set(RULE_DEFAULTS))
    if unknown:
        raise ValueError(f"unknown rule(s): {', '.join(unknown)}")
    rules = {}
    for name, default in RULE_DEFAULTS.items():
        value = raw.get(name, default)
        if name == "passes":
            if not isinstance(value, dict):
                raise ValueError('rules "passes" must be an object')
            bad = sorted(set(value) - set(default))
            if bad:
                renamed = ' ("changes_requested" is now "feedback")' if "changes_requested" in bad else ""
                raise ValueError(f"unknown pass(es): {', '.join(bad)}{renamed}")
            for pass_name, mode in value.items():
                if mode not in PASS_MODES:
                    raise ValueError(f'rules "passes" "{pass_name}" must be "auto", "ask" or false')
            rules[name] = {**default, **value}
        elif name == "instructions":
            if not isinstance(value, dict):
                raise ValueError('rules "instructions" must be an object')
            bad = sorted(set(value) - set(RULE_DEFAULTS["passes"]))
            if bad:
                raise ValueError(f"instructions for unknown pass(es): {', '.join(bad)}")
            if any(not isinstance(text, str) for text in value.values()):
                raise ValueError('rules "instructions" must map each pass to a string')
            rules[name] = dict(value)
        elif name in RULE_PROMPTS:
            if value is True:
                value = default
            if value is not False:
                if (not isinstance(value, dict) or set(value) - {"prompt"} or
                        not isinstance(value.get("prompt", ""), str) or not value.get("prompt", "x").strip()):
                    raise ValueError(f'rule "{name}" must be true, false or {{"prompt": a non-empty string}}')
                value = {**default, **value}
            rules[name] = value
        elif name in RULE_THRESHOLDS:
            key = RULE_THRESHOLDS[name]
            if value is True:
                value = default
            if value is not False:
                if (not isinstance(value, dict) or set(value) - {key} or
                        not isinstance(value.get(key, default[key]), (int, float)) or
                        isinstance(value.get(key), bool) or value.get(key, default[key]) <= 0):
                    raise ValueError(f'rule "{name}" must be false or {{"{key}": a positive number}}')
                value = {**default, **value}
            rules[name] = value
        else:
            if not isinstance(value, bool):
                raise ValueError(f'rule "{name}" must be true or false')
            rules[name] = value
    return rules


REREVIEW_MAX_LINES = 30
REREVIEW_MIN_WAIT_MINUTES = 3
MAX_LINES = {"approve_rereview": REREVIEW_MAX_LINES, "apply_fix": 20}


def validate_auto(raw):
    if not isinstance(raw, dict):
        raise ValueError('"auto" must be an object')
    unknown = sorted(set(raw) - set(AUTO_DEFAULTS) - RETIRED_AUTO)
    if unknown:
        raise ValueError(f"unknown auto action(s): {', '.join(unknown)}")
    auto = {}
    for name in AUTO_DEFAULTS:
        value = raw.get(name, AUTO_DEFAULTS[name])
        if value is not False and name in HARD_LOCKED:
            raise ValueError(f'auto "{name}" is locked: it posts under your name, so it stays manual')
        if isinstance(value, dict):
            allowed = ({"when", "max_per_pr"} | ({"max_lines"} if name in MAX_LINES else set())
                       | ({"min_wait_minutes"} if name == "approve_rereview" else set()))
            if set(value) - allowed:
                raise ValueError(f'auto "{name}" accepts only ' + " and ".join(f'"{k}"' for k in sorted(allowed)))
            when = value.get("when", "confidence >= high")
            level = when.replace("confidence", "").replace(">=", "").strip() if isinstance(when, str) else ""
            if level not in CONFIDENCE:
                raise ValueError(f'auto "{name}" "when" must be "confidence >= low|medium|high"')
            per_pr = value.get("max_per_pr", 2)
            if not isinstance(per_pr, int) or isinstance(per_pr, bool) or per_pr < 1:
                raise ValueError(f'auto "{name}" "max_per_pr" must be a positive integer')
            auto[name] = {"min_confidence": level, "max_per_pr": per_pr}
            if name in MAX_LINES:
                max_lines = value.get("max_lines", MAX_LINES[name])
                if not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines < 0:
                    raise ValueError(f'auto "{name}" "max_lines" must be a non-negative integer')
                auto[name]["max_lines"] = max_lines
            if name == "approve_rereview":
                wait = value.get("min_wait_minutes", REREVIEW_MIN_WAIT_MINUTES)
                if not isinstance(wait, (int, float)) or isinstance(wait, bool) or wait < 0:
                    raise ValueError(f'auto "{name}" "min_wait_minutes" must be a non-negative number')
                auto[name]["min_wait_minutes"] = wait
        elif value is True:
            auto[name] = {"min_confidence": "high", "max_per_pr": 2}
            if name in MAX_LINES:
                auto[name]["max_lines"] = MAX_LINES[name]
            if name == "approve_rereview":
                auto[name]["min_wait_minutes"] = REREVIEW_MIN_WAIT_MINUTES
        elif value is False:
            auto[name] = None
        else:
            raise ValueError(f'auto "{name}" must be true, false, or an object')
    return auto


def validate_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("the top-level value must be a JSON object")
    unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")

    roots = raw.get("roots", DEFAULT_CONFIG["roots"])
    if (not isinstance(roots, list) or
            any(not isinstance(root, str) or not root.strip() for root in roots)):
        raise ValueError('"roots" must be an array of non-empty strings')
    launcher_raw = raw.get("launcher", {})
    if not isinstance(launcher_raw, dict):
        raise ValueError('"launcher" must be an object')
    launcher_unknown = sorted(set(launcher_raw) - set(DEFAULT_LAUNCHER))
    if launcher_unknown:
        raise ValueError(f"unknown launcher setting(s): {', '.join(launcher_unknown)}")
    app = launcher_raw.get("app", DEFAULT_LAUNCHER["app"])
    if app not in LAUNCHER_DEFAULTS:
        raise ValueError(f'launcher "app" must be one of: {", ".join(LAUNCHER_DEFAULTS)}')
    launcher = {"app": app, **LAUNCHER_DEFAULTS[app], **launcher_raw}
    if launcher["target"] not in ("folder", "workspace"):
        raise ValueError('launcher "target" must be either "folder" or "workspace"')
    validate_open_settings("launcher", launcher, "{path}")

    agent = validate_agent(raw.get("agent", DEFAULT_AGENT))
    rules = validate_rules(raw.get("rules", {}))
    auto = validate_auto(raw.get("auto", {}))
    views = validate_views(raw.get("views", []))
    replies = raw.get("approve_replies", APPROVE_REPLIES)
    if not isinstance(replies, list) or any(not isinstance(r, str) or not r.strip() for r in replies):
        raise ValueError('"approve_replies" must be an array of non-empty strings')
    return {"roots": roots, "launcher": launcher, "agent": agent, "rules": rules, "auto": auto, "views": views,
            "approve_replies": replies}


def config_mtime(path=CONFIG_PATH):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def read_raw(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Could not read {CONFIG_FILE}: {e}") from e


def read_config(path=CONFIG_PATH):
    """The validated config; ValueError when the file can't be read or doesn't validate."""
    raw = read_raw(path)
    try:
        return validate_config(raw)
    except ValueError as e:
        raise ValueError(f"Invalid {CONFIG_FILE}: {e}") from e


def format_json(value, depth=0):
    """Two-space JSON that keeps a short list of plain values on one line, the way the file
    is written by hand, so saving a setting doesn't reflow the rest of it."""
    pad = "  " * depth
    if isinstance(value, dict) and value:
        items = [f"{pad}  {json.dumps(k, ensure_ascii=False)}: {format_json(v, depth + 1)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list) and value:
        inline = json.dumps(value, ensure_ascii=False)
        if not any(isinstance(v, (dict, list)) for v in value) and len(pad) + len(inline) <= 90:
            return inline
        return "[\n" + ",\n".join(f"{pad}  {format_json(v, depth + 1)}" for v in value) + f"\n{pad}]"
    return json.dumps(value, ensure_ascii=False)


def write_raw(raw, path=CONFIG_PATH):
    tmp = Path(f"{path}.tmp")
    tmp.write_text(format_json(raw) + "\n")
    os.replace(tmp, path)


def load_config(path=CONFIG_PATH):
    try:
        cfg = read_config(path)
    except ValueError as e:
        sys.exit(str(e))
    for label in ("launcher", "agent"):
        settings = cfg[label]
        if settings and settings["mode"] == "command" and not shutil.which(settings["command"][0]):
            print(f"Warning: {label} command {settings['command'][0]!r} is not on PATH.")
    return cfg


def page_config(cfg):
    """The parts of the config the page needs."""
    return {"launcher": cfg["launcher"], "agent": cfg["agent"], "rules": cfg["rules"],
            "auto": {k: bool(v) for k, v in cfg["auto"].items()},
            "approveRereview": cfg["auto"]["approve_rereview"], "approveReplies": cfg["approve_replies"]}
