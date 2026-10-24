"""Rules: what an event or a PR's state turns into. Pure functions over snapshots and
the "rules" config, so they are unit-tested against fixture PR states.

Two outputs:
  memory_chip()  the chip a PR earns from time or memory (waiting, snoozed);
                 chips that are a pure function of GitHub data live in the page.
  reactions()    what an event sets in motion: a pass, a proposed card, a pin, a
                 review worktree, or a cleanup. Rules never call an agent directly;
                 they hand back a pass for the queue.
"""
from datetime import datetime, timezone

from . import events

# Which pass each event asks for, and which rule switches it off.
EVENT_PASSES = {
    "changes_requested": ("changes_requested", "address_feedback"),
    "ci_failed": ("ci_failed", "fix_ci"),
    "review_requested_of_me": ("pre_review", "review"),
    "review_rerequested": ("re_review", "re_review"),
    "review_dismissed": ("re_review", "re_review"),
}


def parse_time(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def hours_since(iso, now):
    t = parse_time(iso)
    return (now - t).total_seconds() / 3600 if t else None


def waiting_since(snap):
    """When the PR started waiting on its current reviewers: the newest review request
    for someone still requested, else when it left draft, else when it was opened."""
    times = [snap.get("requested_at", {}).get(r) for r in snap.get("requested") or []]
    times = [t for t in times if t]
    return max(times) if times else snap.get("ready_at") or snap.get("created")


def memory_chip(snap, rules, now=None, snoozed_until=None, nudged_at=None):
    now = now or datetime.now(timezone.utc)
    if snoozed_until:
        until = parse_time(snoozed_until)
        if until and until > now:
            return {"kind": "snoozed", "label": f"Snoozed · {until.strftime('%b %-d')}",
                    "until": snoozed_until}
    if snap.get("state") != "OPEN" or not snap.get("mine"):
        return None
    if snap.get("draft"):
        return None
    rule = rules.get("nudge")
    waiting_on = [r for r in snap.get("requested") or []]
    if rule and waiting_on and snap.get("decision") not in ("APPROVED", "CHANGES_REQUESTED"):
        # A nudge restarts the clock, so the chip comes back only after another full wait.
        waited = min((h for h in (hours_since(waiting_since(snap), now), hours_since(nudged_at, now))
                      if h is not None), default=None)
        if waited is not None and waited >= rule["after_hours"]:
            age = f"{int(waited // 24)}d" if waited >= 24 else f"{int(waited)}h"
            return {"kind": "nudge", "label": f"Nudge · {age}", "reviewers": waiting_on}
    return None


def pass_mode(event_name, rules):
    """("pass name", "auto" | "ask") for an event, or None when the rule is off."""
    entry = EVENT_PASSES.get(event_name)
    if not entry:
        return None
    pass_name, rule = entry
    if not rules.get(rule):
        return None
    mode = (rules.get("passes") or {}).get(pass_name, "auto")
    return (pass_name, mode) if mode else None


def reactions(event, snap, rules):
    """The list of things an event sets in motion, as small dicts for the companion."""
    name = event["name"]
    out = []
    if name == "pr_opened":
        out.append({"do": "pin"})
    elif name == "pushed_after_changes_requested" and rules.get("rerequest_review"):
        reviewers = event["data"].get("reviewers") or []
        out.append({"do": "propose", "action": "rerequest_review",
                    "title": f"Re-request {', '.join(reviewers)}'s review",
                    "reason": "You pushed after changes were requested.",
                    "payload": {"reviewers": reviewers}})
    elif name in ("merged", "closed"):
        out.append({"do": "close_cards"})
    elif name == "ci_recovered":
        out.append({"do": "close_cards", "kinds": ["ci_failed"]})
    if name == "review_requested_of_me":
        out.append({"do": "review_worktree"})
    chosen = pass_mode(name, rules)
    if chosen:
        out.append({"do": "pass", "pass": chosen[0], "mode": chosen[1]})
    return out


def card_is_stale(card, snap):
    """A Next card stops applying once the condition that raised it is gone."""
    if snap.get("state") != "OPEN":
        return True
    kind = card.get("pass")
    if kind == "changes_requested":
        asked = set(events.requesters(snap))
        return not events.needs_changes(snap) or bool(asked) and asked <= set(snap.get("requested") or [])
    if kind == "ci_failed":
        return snap.get("ci") == "SUCCESS" or card.get("head") != snap.get("head")
    if kind == "pre_review":
        return not snap.get("requested_me") and bool(snap.get("my_review"))
    if kind == "re_review":
        review = snap.get("my_review") or {}
        return bool(review.get("submitted") and card.get("created") and
                    review["submitted"] > card["created"] and not snap.get("requested_me"))
    if card.get("action") == "rerequest_review":
        wanted = set((card.get("payload") or {}).get("reviewers") or [])
        return bool(wanted) and wanted <= set(snap.get("requested") or [])
    return False
