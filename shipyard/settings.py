"""The settings page's fields: each setting in shipyard.config.json, its default, and how
the page reads and writes it.

A save edits the file's own JSON at one path and validates the result; it never writes
back the validated config, which has every default filled in (and, for `auto`, a
different shape). So the file keeps only what was changed, and a default stays a default.
"""
import copy
import shlex

from .agents import ADAPTERS
from .config import (CONFIG_FILE, CONFIG_PATH, HARD_LOCKED, LAUNCHER_DEFAULTS, read_raw, validate_auto, validate_config,
                     validate_views, write_raw)

SECTIONS = [
    {"id": "launcher", "title": "Opening checkouts",
     "help": "The app or IDE a checkout opens in."},
    {"id": "agent", "title": "Coding agent",
     "help": "The agent whose sessions Shipyard resumes, and that runs its passes."},
    {"id": "rules", "title": "Next steps",
     "help": "Which next steps show on cards. Turning one off also stops its pass."},
    {"id": "passes", "title": "Agent passes",
     "help": "When the agent looks at a PR on its own. \"Wait for Ask\" runs it only when you ask in the sidebar."},
    {"id": "auto", "title": "Automatic actions",
     "help": "Shipyard presses these buttons for you when the agent's proposal is at least this confident. "
             "Cards say when an action holds back, and offer Undo where they can."},
    {"id": "roots", "title": "Folders to scan", "help": "Where Shipyard looks for your Git clones."},
]

OPEN_MODES = [["url", "Open a URL"], ["command", "Run a command"]]
PASS_MODES = [["auto", "Run on its own"], ["ask", "Wait for Ask"], ["off", "Off"]]
CONFIDENCE_LEVELS = [["high", "High"], ["medium", "Medium"], ["low", "Low"]]
AGENT_NAMES = {"claude-code": "Claude Code", "codex": "Codex"}
AUTO_LABELS = {"rerun_checks": "Re-run flaky checks", "enable_auto_merge": "Turn on auto-merge",
               "approve_rereview": "Approve a re-review", "apply_fix": "Apply and push a CI fix",
               "post_reply": "Post a reply"}


def _command_text(args):
    """A command as you'd type it: quoting only where an argument needs it ({path} doesn't)."""
    return " ".join(a if a and not any(c.isspace() or c in "'\"\\" for c in a) else shlex.quote(a) for a in args)


def _agent(cfg):
    return cfg["agent"] or validate_config({})["agent"]


def _auto(cfg, name):
    return cfg["auto"][name] or validate_auto({name: True})[name]


def _rule_option(cfg, name, key):
    value = cfg["rules"][name]
    return (value if isinstance(value, dict) else validate_config({})["rules"][name])[key]


def _field(section, path, kind, label, get, help="", choices=None, parent=None, raw_key=None, to_raw=None,
           group=None):
    """`group` "advanced" marks a setting the choice above it fills in, shown under its own heading."""
    return {"section": section, "path": path, "kind": kind, "label": label, "help": help, "choices": choices,
            "parent": parent, "raw_key": raw_key or path[-1], "get": get, "to_raw": to_raw or (lambda v: v),
            "group": group}


def _open_fields(section, placeholder):
    """The url-or-command settings the launcher and the agent share."""
    root = (section,)
    parent = root if section == "agent" else None
    settings = (lambda cfg: _agent(cfg)) if section == "agent" else (lambda cfg: cfg["launcher"])
    return [
        _field(section, root + ("mode",), "choice", "How to open", lambda cfg: settings(cfg)["mode"],
               choices=OPEN_MODES, parent=parent, group="advanced"),
        _field(section, root + ("url",), "text", "URL", lambda cfg: settings(cfg)["url"], parent=parent,
               group="advanced", help=f"Used when opening a URL. {placeholder} stands for the "
                    + ("session." if section == "agent" else "checkout path.")),
        _field(section, root + ("command",), "command", "Command", lambda cfg: _command_text(settings(cfg)["command"]),
               parent=parent, to_raw=shlex.split, group="advanced",
               help=f"Used when running a command. Must include {placeholder}."),
    ]


FIELDS = [
    _field("views", ("views",), "views", "Views", lambda cfg: cfg["views"], to_raw=validate_views),

    _field("launcher", ("launcher", "app"), "choice", "App", lambda cfg: cfg["launcher"]["app"],
           choices=[[k, v["name"]] for k, v in LAUNCHER_DEFAULTS.items()],
           help="Fills in everything under Advanced. Opening by command needs the app's command-line tool on your PATH."),
    *_open_fields("launcher", "{path}"),
    _field("launcher", ("launcher", "target"), "choice", "Open", lambda cfg: cfg["launcher"]["target"],
           choices=[["workspace", "The .code-workspace file when there is one"], ["folder", "The folder"]],
           group="advanced"),
    _field("launcher", ("launcher", "name"), "text", "App name", lambda cfg: cfg["launcher"]["name"],
           group="advanced", help="What buttons call it."),

    _field("agent", ("agent",), "switch", "Use a coding agent", lambda cfg: cfg["agent"] is not None,
           help="Off hides the agent buttons and stops passes."),
    _field("agent", ("agent", "sessions"), "choice", "Agent", lambda cfg: _agent(cfg)["sessions"],
           choices=[[k, AGENT_NAMES.get(k, k)] for k in ADAPTERS], parent=("agent",),
           help="Where Shipyard finds your sessions, and which CLI runs passes. Fills in everything under Advanced."),
    *_open_fields("agent", "{session}"),
    _field("agent", ("agent", "prompt_url"), "optional", "New session URL",
           lambda cfg: _agent(cfg)["prompt_url"] or "", parent=("agent",), to_raw=lambda v: v or None, group="advanced",
           help="Opens a new session with {prompt} typed in, for the Address button. Empty for none."),
    _field("agent", ("agent", "name"), "text", "Agent name", lambda cfg: _agent(cfg)["name"], parent=("agent",),
           group="advanced", help="What buttons call it."),

    *[_field("rules", ("rules", name), "switch", label, lambda cfg, name=name: cfg["rules"][name], help=help)
      for name, label, help in [
          ("fix_ci", "Fix CI", "When a check fails on your PR."),
          ("merge", "Merge", "When your PR is approved and green."),
          ("review", "Review", "When your review is requested."),
          ("re_review", "Re-review", "When a PR you reviewed is ready for another look."),
          ("rerequest_review", "Re-request review", "When you've pushed or replied after changes were requested.")]],
    _field("rules", ("rules", "address_feedback"), "switch", "Address feedback",
           lambda cfg: cfg["rules"]["address_feedback"] is not False, help="When a reviewer requests changes."),
    _field("rules", ("rules", "address_feedback", "prompt"), "text", "Address prompt",
           lambda cfg: _rule_option(cfg, "address_feedback", "prompt"), parent=("rules", "address_feedback"),
           help="Typed into a new session by the Address button: {url}, {repo}, {number}, {branch}."),
    _field("rules", ("rules", "nudge"), "switch", "Nudge", lambda cfg: cfg["rules"]["nudge"] is not False,
           help="When your PR has waited on reviewers for a while."),
    _field("rules", ("rules", "nudge", "after_hours"), "number", "Nudge after (hours)",
           lambda cfg: _rule_option(cfg, "nudge", "after_hours"), parent=("rules", "nudge")),

    *[_field("passes", ("rules", "passes", name), "choice", label,
             lambda cfg, name=name: cfg["rules"]["passes"][name] or "off", choices=PASS_MODES,
             to_raw=lambda v: False if v == "off" else v)
      for name, label in [("feedback", "Changes requested on your PR"), ("ci_failed", "CI failed on your PR"),
                          ("pre_review", "Your review is requested"), ("re_review", "A PR you reviewed changed")]],

    *[f for name in ("rerun_checks", "enable_auto_merge", "approve_rereview", "apply_fix") for f in [
        _field("auto", ("auto", name), "switch", AUTO_LABELS[name], lambda cfg, name=name: cfg["auto"][name] is not None),
        _field("auto", ("auto", name, "when"), "choice", "Minimum confidence",
               lambda cfg, name=name: _auto(cfg, name)["min_confidence"], choices=CONFIDENCE_LEVELS,
               parent=("auto", name), to_raw=lambda v: f"confidence >= {v}"),
        _field("auto", ("auto", name, "max_per_pr"), "number", "At most, per PR",
               lambda cfg, name=name: _auto(cfg, name)["max_per_pr"], parent=("auto", name)),
        *([_field("auto", ("auto", name, "max_lines"), "number", "Largest change (lines)",
                  lambda cfg, name=name: _auto(cfg, name)["max_lines"], parent=("auto", name),
                  help="Holds back when the change touches more lines than this.")]
          if name in ("approve_rereview", "apply_fix") else []),
        *([_field("auto", ("auto", name, "min_wait_minutes"), "number", "Wait after a push (minutes)",
                  lambda cfg, name=name: _auto(cfg, name)["min_wait_minutes"], parent=("auto", name))]
          if name == "approve_rereview" else []),
    ]],
    *[_field("auto", ("auto", name), "locked", AUTO_LABELS[name], lambda cfg: False,
             help="It puts words in front of a colleague under your name.")
      for name in sorted(HARD_LOCKED)],

    _field("roots", ("roots",), "lines", "Folders", lambda cfg: "\n".join(cfg["roots"]),
           to_raw=lambda v: [line.strip() for line in v.splitlines() if line.strip()],
           help="One per line. Applies after Shipyard restarts. Folders passed to shipyard.py take their place."),
]
BY_PATH = {f["path"]: f for f in FIELDS}
SWITCHED = {f["parent"] for f in FIELDS if f["parent"]}  # settings that are false, true, or an object of options
MISSING = object()


def _lookup(raw, path):
    node = raw
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return MISSING
        node = node[key]
    return node


def _put(raw, path, value):
    node = raw
    for key in path[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    node[path[-1]] = value


def _drop(raw, path):
    """Delete the key at `path`, and any parent object it leaves empty."""
    parent = _lookup(raw, path[:-1]) if len(path) > 1 else raw
    if isinstance(parent, dict):
        parent.pop(path[-1], None)
        if not parent and len(path) > 1:
            _drop(raw, path[:-1])


def _without(raw, field):
    raw = copy.deepcopy(raw)
    if field["parent"]:
        holder = _lookup(raw, field["parent"])
        if isinstance(holder, dict):
            holder.pop(field["raw_key"], None)
    else:
        _drop(raw, field["path"])
    return raw


def _is_set(raw, field):
    if field["parent"]:
        holder = _lookup(raw, field["parent"])
        return isinstance(holder, dict) and field["raw_key"] in holder
    return _lookup(raw, field["path"]) is not MISSING


def fields(raw):
    """Every field with its current value, its default, and whether the file sets it."""
    cfg = validate_config(raw)
    out = []
    for f in FIELDS:
        try:
            default = f["get"](validate_config(_without(raw, f)))
        except ValueError:
            default = None
        value = f["get"](cfg)
        is_set = _is_set(raw, f)
        if f["path"] in SWITCHED and isinstance(_lookup(raw, f["path"]), dict):
            is_set = value != default  # options alone don't change whether it's on
        out.append({"section": f["section"], "path": list(f["path"]), "kind": f["kind"], "label": f["label"],
                    "help": f["help"], "choices": f["choices"], "parent": list(f["parent"]) if f["parent"] else None,
                    "group": f["group"], "value": value, "default": default, "set": is_set})
    return out


def _enabled_by_default(raw, path):
    probe = copy.deepcopy(raw)
    _drop(probe, path)
    return BY_PATH[path]["get"](validate_config(probe))


def apply(raw, path, value=None, reset=False):
    """`raw` with one field set to `value`, or back to its default; the input is left alone."""
    field = BY_PATH.get(tuple(path))
    if not field or field["kind"] == "locked":
        raise ValueError(f"That setting can only be changed in {CONFIG_FILE}.")
    raw = copy.deepcopy(raw)
    path = field["path"]
    if path in SWITCHED:
        current = _lookup(raw, path)
        if reset:
            _drop(raw, path)
        elif not value:
            _put(raw, path, None if path == ("agent",) else False)
        elif not isinstance(current, dict):
            _drop(raw, path)  # back to the default options
            if not _enabled_by_default(raw, path):
                _put(raw, path, True)
    elif field["parent"]:
        holder = _lookup(raw, field["parent"])
        if holder is False or holder is None:
            raise ValueError("Turn it on first.")
        if reset:
            if isinstance(holder, dict):
                holder.pop(field["raw_key"], None)
                if not holder:
                    _drop(raw, field["parent"])
                    if not _enabled_by_default(raw, field["parent"]):
                        _put(raw, field["parent"], True)
        else:
            if not isinstance(holder, dict):
                holder = {}
                _put(raw, field["parent"], holder)
            holder[field["raw_key"]] = field["to_raw"](value)
    elif reset:
        _drop(raw, path)
    else:
        _put(raw, path, field["to_raw"](value))
    return raw


def save(path, value=None, reset=False, expect=MISSING, config_path=CONFIG_PATH):
    """Change one setting in the file and return (the new config, the new fields).
    `expect` is the value the page last showed; a hand edit made since then wins."""
    raw = read_raw(config_path)
    if expect is not MISSING:
        shown = next((f["value"] for f in fields(raw) if tuple(f["path"]) == tuple(path or ())), MISSING)
        if shown != expect:
            raise ValueError(f"That setting changed in {CONFIG_FILE} since this page loaded. Reload and try again.")
    new = apply(raw, path or (), value, reset)
    try:
        cfg = validate_config(new)
    except ValueError as e:
        raise ValueError(f"Invalid setting: {e}") from e
    write_raw(new, config_path)
    return cfg, fields(new)
