"""Rules: what an event or a PR's state turns into. Pure functions over snapshots and
the "rules" config, so they are unit-tested against fixture PR states.

Three outputs:
  next_steps()   the next steps a PR's GitHub state calls for; the page shows the
                 first as the card's chip and draws nothing of its own.
  memory_chip()  the chip a PR earns from time or memory (waiting, snoozed).
  reactions()    what an event sets in motion: a pass, a proposed card, a pin, a
                 review worktree, or a cleanup. Rules never call an agent directly;
                 they hand back a pass for the queue.
"""
from datetime import datetime, timezone

from . import events

# Which pass each event asks for, and which rule switches it off.
EVENT_PASSES = {
    "feedback": ("feedback", "address_feedback"),
    "ci_failed": ("ci_failed", "fix_ci"),
    "review_requested_of_me": ("pre_review", "review"),
    "review_rerequested": ("re_review", "re_review"),
    "review_dismissed": ("re_review", "re_review"),
}


# The steps that keep each pass's card current; with none of them left, the card is done.
STEPS_FOR_PASS = {
    "feedback": ("address_feedback", "rerequest_review"),
    "ci_failed": ("fix_ci",),
    "pre_review": ("review",),
    "re_review": ("re_review",),
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


def author_moved_at(snap):
    """When the author last pushed or commented after your changes request, while it still
    stands and they haven't asked you to look again; None otherwise."""
    review = snap.get("my_review") or {}
    since = review.get("submitted")
    if review.get("state") != "CHANGES_REQUESTED" or snap.get("requested_me") or not since:
        return None
    author = snap.get("author")
    times = [c.get("created") for t in snap.get("threads") or [] for c in t.get("comments") or []
             if c.get("author") == author]
    times += [c.get("created") for c in snap.get("comments") or [] if c.get("author") == author]
    if review.get("commit") != snap.get("head"):
        times.append(snap.get("last_commit_at"))
    times = [t for t in times if t and t > since]
    return max(times) if times else None


def memory_chip(snap, rules, now=None, snoozed_until=None, nudged_at=None):
    now = now or datetime.now(timezone.utc)
    if snoozed_until:
        until = parse_time(snoozed_until)
        if until and until > now:
            return {"kind": "snoozed", "label": f"Snoozed · {until.strftime('%b %-d')}",
                    "until": snoozed_until}
    if snap.get("state") != "OPEN" or snap.get("draft"):
        return None
    if not snap.get("mine"):
        rule = rules.get("ask_rereview")
        waited = hours_since(author_moved_at(snap), now)
        if rule and waited is not None and waited >= rule["after_hours"]:
            age = f"{int(waited // 24)}d" if waited >= 24 else f"{int(waited)}h"
            return {"kind": "rerequest_mine", "label": f"Re-request review · {age}", "author": snap.get("author")}
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


def _clip(text, n):
    return f"{text[:n - 1]}…" if len(text) > n else text


def _approval_step(snap, rules):
    approvers = [r["author"] for r in events.approval_feedback(snap, rules.get("image_sign_off", True))]
    if approvers and rules.get("address_feedback"):
        return {"kind": "address_feedback", "label": f"Reply to approval · {len(approvers)}", "tone": "gold",
                "reviewers": approvers, "approval": True}
    return None


def next_steps(snap, rules, cards=()):
    """Every next step the PR's GitHub state calls for, most urgent first. Each rule
    gates its own step; `cards` lends the re-review its progress."""
    if snap.get("dropped"):
        return []
    if snap.get("state") == "MERGED":
        step = snap.get("mine") and _approval_step(snap, rules)
        return [step] if step else []
    if snap.get("state") != "OPEN":
        return []
    out = []
    if not snap.get("mine"):
        review = snap.get("my_review")
        requested = snap.get("requested_me")
        if requested and not review and rules.get("review"):
            out.append({"kind": "review", "label": "Review", "tone": "gold"})
        if ((requested and review) or (review or {}).get("state") == "DISMISSED") and rules.get("re_review"):
            card = next((c for c in cards if c.get("pass") == "re_review" and c.get("total")), None)
            out.append({"kind": "re_review", "tone": "gold",
                        "label": f"Re-review · {card['addressed']} of {card['total']} addressed" if card else "Re-review"})
        return out
    pending = set(snap.get("requested") or [])
    waiting = [a for a in dict.fromkeys(events.requesters(snap)) if a not in pending]
    if waiting:
        commits = {r["author"]: r.get("commit") for r in snap.get("reviews") or []}
        pushed = all(commits.get(a) and commits[a] != snap.get("head") for a in waiting)
        open_threads = [t for t in snap.get("threads") or [] if not t.get("resolved")]
        replied = bool(open_threads) and all((t.get("comments") or [{}])[-1].get("author") == snap.get("viewer")
                                             for t in open_threads)
        if (pushed or replied) and rules.get("rerequest_review"):
            out.append({"kind": "rerequest_review", "label": f"Re-request review · {len(waiting)}", "tone": "gold",
                        "reviewers": waiting,
                        "title": "You pushed since the review" if pushed else "You replied last on every open thread"})
        elif rules.get("address_feedback"):
            out.append({"kind": "address_feedback", "label": f"Address feedback · {len(waiting)}", "tone": "red",
                        "reviewers": waiting})
    approval = _approval_step(snap, rules)
    if approval:
        out.append(approval)
    conflicting = snap.get("mergeable") == "CONFLICTING"
    if conflicting and rules.get("resolve_conflicts"):
        out.append({"kind": "resolve_conflicts", "label": "Resolve conflicts", "tone": "red",
                    "title": f"Conflicts with {snap.get('base')}"})
    behind = snap.get("behind_parent")
    if behind and not conflicting and rules.get("update_stack"):
        out.append({"kind": "update_branch", "label": f"Update from #{snap['stack_parent']}", "tone": "gold",
                    "behind": behind, "parent": snap["stack_parent"],
                    "title": f"#{snap['stack_parent']} has {behind} commit{'s' if behind != 1 else ''} this branch doesn't"})
    if snap.get("ci") in events.FAILING and rules.get("fix_ci"):
        names = [c["name"] for c in snap.get("failing") or [] if c.get("name")]
        out.append({"kind": "fix_ci", "tone": "red", "title": ", ".join(names),
                    "label": f"Fix CI · {_clip(names[0], 18)}" if names else "Fix CI"})
    if (snap.get("decision") == "APPROVED" and snap.get("ci") in (None, "SUCCESS") and not snap.get("auto_merge")
            and not snap.get("draft") and not conflicting and rules.get("merge")):
        out.append({"kind": "merge", "label": "Merge", "tone": "green"})
    return out


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
    """The list of things an event sets in motion, as small dicts for the server."""
    name = event["name"]
    out = []
    if name == "pr_opened":
        out.append({"do": "pin"})
    elif name == "pushed_after_feedback" and rules.get("rerequest_review"):
        reviewers = event["data"].get("reviewers") or []
        out.append({"do": "propose", "action": "rerequest_review",
                    "title": f"Re-request {', '.join(reviewers)}'s review",
                    "reason": "You pushed after changes were requested.",
                    "payload": {"reviewers": reviewers}})
    elif name == "merged" and events.approval_feedback(snap, rules.get("image_sign_off", True)):
        out.append({"do": "close_cards", "except": ["feedback"]})
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


def card_is_stale(card, snap, rules):
    """A Next card stops applying once no next step calls for it."""
    kinds = STEPS_FOR_PASS.get(card.get("pass"))
    if kinds:
        if card.get("pass") == "ci_failed" and card.get("head") != snap.get("head"):
            return True
        return not any(s["kind"] in kinds for s in next_steps(snap, rules))
    if snap.get("state") != "OPEN":
        return True
    if card.get("action") == "rerequest_review":
        wanted = set((card.get("payload") or {}).get("reviewers") or [])
        return bool(wanted) and wanted <= set(snap.get("requested") or [])
    return False
