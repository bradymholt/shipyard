"""The settings page's fields: each setting in shipyard.config.json, its default, and how
the page reads and writes it.

A save edits the file's own JSON at one path and validates the result; it never writes
back the validated config, which has every default filled in (and, for `auto`, a
different shape). So the file keeps only what was changed, and a default stays a default.
"""
import copy
import shlex

from .agents import ADAPTERS
from .config import (AGENT_DEFAULTS, CONFIG_FILE, CONFIG_PATH, HARD_LOCKED, LAUNCHER_DEFAULTS, read_raw, validate_auto,
                     validate_config, validate_views, write_raw)

SECTIONS = [
    {"id": "launcher", "title": "Opening checkouts",
     "help": "The app or IDE a checkout opens in."},
    {"id": "agent", "title": "Coding agent",
     "help": "The agent whose sessions Shipyard resumes, and that runs its passes."},
    {"id": "rules", "title": "Workflows", "cards": True,
     "help": "Which next steps show on cards, the agent pass each one starts, and what Shipyard does on its own "
             "when a pass is confident enough. Auto runs a pass on its own; Ask waits until you ask in the sidebar. "
             "Cards say when an automatic action holds back, and offer Undo where they can."},
    {"id": "roots", "title": "Folders to scan", "help": "Where Shipyard looks for your Git clones."},
]

OPEN_MODES = [["url", "Open a URL"], ["command", "Run a command"]]
PASS_MODES = [["auto", "Auto"], ["ask", "Ask"], ["off", "Off"]]
CONFIDENCE_LEVELS = [["high", "High"], ["medium", "Medium"], ["low", "Low"]]
AGENT_NAMES = {"claude-code": "Claude Code", "codex": "Codex"}
AUTO_LABELS = {"rerun_checks": "Automatically re-run flaky checks",
               "approve_rereview": "Automatically approve the re-review", "apply_fix": "Automatically push small CI fixes",
               "post_reply": "Post a reply"}
AUTO_HELP = {"post_reply": "A reply puts words in front of a colleague under your name."}
# Each pass, the next step that starts it (EVENT_PASSES in rules.py), and the automatic
# actions only its proposals can lead to (AUTO_POLICY in orchestrator.py).
PASS_GROUPS = [
    ("ci_failed", "fix_ci", "", ("rerun_checks", "apply_fix")),
    ("feedback", "address_feedback", "", ("post_reply",)),
    ("pre_review", "review", "Pre-reviews stay drafts only you can see; submitting is yours.", ()),
    ("re_review", "re_review", "", ("approve_rereview",)),
]


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
           group=None, under=None, heading=None, sentence=None):
    """`group` "advanced" marks a setting the choice above it fills in, shown under its own heading.
    `heading` starts a titled group of settings on the page. `sentence` replaces the help while
    the setting is on, with each {key} an inline control for the child setting of that name.
    `under` nests a setting under another on the page without making it part of it in the file."""
    return {"section": section, "path": path, "kind": kind, "label": label, "help": help, "choices": choices,
            "parent": parent, "raw_key": raw_key or path[-1], "get": get, "to_raw": to_raw or (lambda v: v),
            "group": group, "under": under, "heading": heading,
            "sentence": sentence}


def _open_fields(section, placeholder):
    """The url-or-command settings the launcher and the agent share."""
    root = (section,)
    parent = root if section == "agent" else None
    settings = (lambda cfg: _agent(cfg)) if section == "agent" else (lambda cfg: cfg["launcher"])
    return [
        _field(section, root + ("mode",), "choice", "How to open", lambda cfg: settings(cfg)["mode"],
               choices=OPEN_MODES, parent=parent, group="advanced"),
        _field(section, root + ("url",), "text", "URL", lambda cfg: settings(cfg)["url"], parent=parent,
               group="advanced", help=f"{placeholder} stands for the "
                    + ("session." if section == "agent" else "checkout path.")),
        _field(section, root + ("command",), "command", "Command", lambda cfg: _command_text(settings(cfg)["command"]),
               parent=parent, to_raw=shlex.split, group="advanced",
               help=f"Must include {placeholder}."),
    ]


def _auto_fields(name, under):
    """An automatic action, shown under the pass whose proposals it can act on."""
    if name in HARD_LOCKED:
        return [_field("rules", ("auto", name), "locked", AUTO_LABELS[name], lambda cfg: False, under=under,
                       help=AUTO_HELP[name])]
    sentence = ("At {when} confidence or above, at most {max_per_pr} per PR"
                + (", up to {max_lines} changed lines" if name in ("approve_rereview", "apply_fix") else "")
                + (", {min_wait_minutes} minutes after a push" if name == "approve_rereview" else "") + ".")
    return [
        _field("rules", ("auto", name), "switch", AUTO_LABELS[name], lambda cfg: cfg["auto"][name] is not None,
               under=under, help=AUTO_HELP.get(name, ""),
               sentence=sentence),
        _field("rules", ("auto", name, "when"), "choice", "Minimum confidence",
               lambda cfg: _auto(cfg, name)["min_confidence"], choices=CONFIDENCE_LEVELS,
               parent=("auto", name), to_raw=lambda v: f"confidence >= {v}"),
        _field("rules", ("auto", name, "max_per_pr"), "number", "At most, per PR",
               lambda cfg: _auto(cfg, name)["max_per_pr"], parent=("auto", name)),
        *([_field("rules", ("auto", name, "max_lines"), "number", "Largest change (lines)",
                  lambda cfg: _auto(cfg, name)["max_lines"], parent=("auto", name))]
          if name in ("approve_rereview", "apply_fix") else []),
        *([_field("rules", ("auto", name, "min_wait_minutes"), "number", "Wait after a push (minutes)",
                  lambda cfg: _auto(cfg, name)["min_wait_minutes"], parent=("auto", name))]
          if name == "approve_rereview" else []),
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

    _field("rules", ("rules", "fix_ci"), "switch", "Fix CI", lambda cfg: cfg["rules"]["fix_ci"],
           help="When a check fails on your PR.", heading="My PRs"),
    _field("rules", ("rules", "address_feedback"), "switch", "Address feedback",
           lambda cfg: cfg["rules"]["address_feedback"] is not False, help="When a reviewer requests changes."),
    _field("rules", ("rules", "address_feedback", "prompt"), "text", "Address prompt",
           lambda cfg: _rule_option(cfg, "address_feedback", "prompt"), parent=("rules", "address_feedback"),
           help="Typed into a new session by the Address button: {url}, {repo}, {number}, {branch}."),
    _field("rules", ("rules", "rerequest_review"), "switch", "Re-request review",
           lambda cfg: cfg["rules"]["rerequest_review"],
           help="When you've pushed or replied after changes were requested."),
    _field("rules", ("rules", "nudge"), "switch", "Nudge", lambda cfg: cfg["rules"]["nudge"] is not False,
           help="When your PR has waited on reviewers for a while.",
           sentence="When your PR has waited on reviewers for {after_hours} hours."),
    _field("rules", ("rules", "nudge", "after_hours"), "number", "Nudge after (hours)",
           lambda cfg: _rule_option(cfg, "nudge", "after_hours"), parent=("rules", "nudge")),
    _field("rules", ("rules", "merge"), "switch", "Merge", lambda cfg: cfg["rules"]["merge"],
           help="When your PR is approved and green."),
    _field("rules", ("rules", "review"), "switch", "Review", lambda cfg: cfg["rules"]["review"],
           help="When your review is requested.", heading="Reviews"),
    _field("rules", ("rules", "re_review"), "switch", "Re-review", lambda cfg: cfg["rules"]["re_review"],
           help="When a PR you reviewed is ready for another look."),

    *[f for name, step, help, actions in PASS_GROUPS for f in [
        _field("rules", ("rules", "passes", name), "segments", "Agent pass",
               lambda cfg, name=name: cfg["rules"]["passes"][name] or "off", choices=PASS_MODES,
               to_raw=lambda v: False if v == "off" else v, help=help, under=("rules", step)),
        *[f for action in actions for f in _auto_fields(action, ("rules", "passes", name))],
    ]],

    _field("roots", ("roots",), "lines", "Folders", lambda cfg: "\n".join(cfg["roots"]),
           to_raw=lambda v: [line.strip() for line in v.splitlines() if line.strip()],
           help="One per line, each a folder that holds your Git clones. Takes effect right away."),
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
                    "group": f["group"], "under": list(f["under"]) if f["under"] else None,
                    "heading": f["heading"], "sentence": f["sentence"], "value": value, "default": default, "set": is_set})
    return out


def _enabled_by_default(raw, path):
    probe = copy.deepcopy(raw)
    _drop(probe, path)
    return BY_PATH[path]["get"](validate_config(probe))


# An app or an agent brings its own defaults for the settings under it. Switching drops any
# of those still equal to the old choice's default: that was a default pinned in the file,
# not a choice, and the new one should show through. Settings you changed stay.
PRESETS = {("launcher", "app"): LAUNCHER_DEFAULTS, ("agent", "sessions"): AGENT_DEFAULTS}


def _unpin_old_preset(old_raw, raw, path):
    section = _lookup(raw, path[:-1])
    if not isinstance(section, dict):
        return
    try:
        old, new = (BY_PATH[path]["get"](validate_config(r)) for r in (old_raw, raw))
    except ValueError:
        return  # the save itself reports what's wrong
    if old == new:
        return
    pinned = PRESETS[path][old]
    for key in [k for k, v in section.items() if k != path[-1] and k in pinned and pinned[k] == v]:
        del section[key]


def apply(raw, path, value=None, reset=False):
    """`raw` with one field set to `value`, or back to its default; the input is left alone."""
    field = BY_PATH.get(tuple(path))
    if not field or field["kind"] == "locked":
        raise ValueError(f"That setting can only be changed in {CONFIG_FILE}.")
    if field["path"] in PRESETS:
        new = _apply(raw, field, value, reset)
        _unpin_old_preset(raw, new, field["path"])
        return new
    return _apply(raw, field, value, reset)


def _apply(raw, field, value, reset):
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
