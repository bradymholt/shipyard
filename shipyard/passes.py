"""Passes: a bounded reading task with a structured answer. The server fills a
template from shipyard/passes/, runs the agent headlessly through its adapter, and
parses the answer into a Next card. The agent proposes; it never acts.

How a pass runs:
  - your PR, a question (ci_failed, ask): a fork of the pinned session, so the history
    is there and nothing is written back. Agents that cannot fork run a fresh session.
  - your PR, a plan you will continue (feedback): a resume of the pinned
    session, so the plan is in the conversation when you open it.
  - someone else's PR (pre_review): a fresh session in a review worktree, pinned.
  - any PR, on request (new_session): a fresh session to keep talking in, pinned; it is
    told where the earlier pinned session's transcript is.
    re_review resumes that session.
A resume never adds a turn to a session that is live: it runs in a fork of it (or a fresh
session, for agents that can't fork), which is kept and linked from the card.
"""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent / "passes"
TEMPLATE_FILES = {
    "feedback": "feedback.md",
    "ci_failed": "ci-failed.md",
    "pre_review": "pre-review.md",
    "re_review": "re-review.md",
    "ask": "ask.md",
    "new_session": "new-session.md",
}
# fork: question; resume: plan to continue; fresh: new pinned session.
PASS_MODES = {"feedback": "resume", "ci_failed": "fork", "pre_review": "fresh",
              "re_review": "resume", "ask": "fork", "new_session": "fresh"}
ACTIONS = ["continue", "post_reply", "apply_fix", "rerun_checks", "rerequest_review", "approve",
           "comment_unresolved", "submit_review", "enable_auto_merge", "merge", "mark_ready", "nudge"]

ANSWER_SCHEMA = {
    "type": "object",
    "required": ["summary", "proposals"],
    "properties": {
        "summary": {"type": "string"},
        "addressed": {"type": "integer"},
        "total": {"type": "integer"},
        "auto_approve": {
            "type": "object",
            "required": ["on", "why"],
            "properties": {"on": {"type": "boolean"}, "why": {"type": "string"}},
        },
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action", "confidence", "reason"],
                "properties": {
                    "action": {"type": "string", "enum": ACTIONS},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "reason": {"type": "string"},
                    "payload": {"type": "object"},
                },
            },
        },
    },
}
TITLES = {"feedback": "Address feedback", "ci_failed": "Fix CI",
          "pre_review": "Pre-review", "re_review": "Re-review", "ask": "Answer", "new_session": "New session"}


class SafeDict(dict):
    def __missing__(self, key):
        return ""


def render(name, values):
    text = (TEMPLATES / TEMPLATE_FILES[name]).read_text()
    return text.format_map(SafeDict(values))


def feedback_text(snap):
    lines = []
    threads = sorted(snap.get("threads") or [], key=lambda t: t["resolved"])
    for t in threads:
        if t["resolved"] and len(lines) > 40:
            break
        first = (t.get("comments") or [{}])[0]
        where = f"{t.get('path')}:{t.get('line') or '?'}"
        state = "resolved" if t["resolved"] else "outdated" if t["outdated"] else "open"
        lines.append(f"- [{state}] comment_id={first.get('id')} {where} @{first.get('author')}: "
                     f"{' '.join((first.get('body') or '').split())[:600]}")
        for reply in (t.get("comments") or [])[1:4]:
            lines.append(f"    reply @{reply.get('author')}: {' '.join((reply.get('body') or '').split())[:300]}")
    for r in snap.get("reviews") or []:
        if r.get("body"):
            lines.append(f"- review by @{r['author']} ({r['state']}): {' '.join(r['body'].split())[:800]}")
    return "\n".join(lines) or "(no comments; the review had no text)"


def my_threads_text(snap):
    viewer = snap.get("viewer")
    lines = []
    for t in snap.get("threads") or []:
        first = (t.get("comments") or [{}])[0]
        if first.get("author") != viewer:
            continue
        state = "resolved" if t["resolved"] else "open"
        lines.append(f"- [{state}] {t.get('path')}:{t.get('line') or '?'}: "
                     f"{' '.join((first.get('body') or '').split())[:500]}")
    return "\n".join(lines) or "(no line comments; only the review summary)"


def inputs(name, snap, github=None, question=None):
    """Template values for a pass: summarized, so a few hundred tokens land per event."""
    values = {k: snap.get(k) for k in ("repo", "number", "title", "url", "branch", "head", "base",
                                       "author")}
    values["body"] = (snap.get("body") or "(no description)")[:3000]
    if name == "feedback":
        values["feedback"] = feedback_text(snap)
    elif name == "ci_failed":
        failing = snap.get("failing") or []
        values["checks"] = "\n".join(f"- {c['name']} ({c.get('url') or 'no link'})" for c in failing) or "(unknown)"
        logs = []
        for c in failing[:2]:
            if github and c.get("job_id") and c.get("run_id"):
                tail = github.job_log_tail(snap["repo"], c["job_id"])
                if tail:
                    logs.append(f"== {c['name']} ==\n{tail}")
        values["log"] = "\n\n".join(logs) or "(log not available; use `gh run view --log-failed`)"
    elif name == "re_review":
        review = snap.get("my_review") or {}
        values["reviewed_commit"] = review.get("commit") or "unknown"
        values["review_state"] = review.get("state") or "unknown"
        values["threads"] = my_threads_text(snap)
    elif name == "ask":
        values["question"] = question or ""
    elif name == "new_session":
        values["state"] = (snap.get("state") or "").lower()
        values["earlier"] = question or "(none found)"
    return values


def raw_event_text(name, snap):
    """What to paste into a session yourself when Shipyard can't run the pass."""
    head = f"{snap['repo']}#{snap['number']} {snap['url']}"
    if name == "feedback":
        return f"Review feedback on {head}:\n{feedback_text(snap)}"
    if name == "ci_failed":
        checks = ", ".join(c["name"] for c in snap.get("failing") or []) or "checks"
        return f"CI failed on {head}: {checks}. Find the cause and fix it."
    if name == "re_review":
        return f"Re-review {head}: check my earlier threads against the new commits.\n{my_threads_text(snap)}"
    return f"Shipyard event {name} on {head}."


def fix_message(reason):
    """A commit subject from a fix's reason, for an answer that gave no message."""
    subject = re.split(r"[.;,:]\s|\s[-\u2013\u2014]\s", reason.strip(), maxsplit=1)[0].strip().rstrip(".")
    if len(subject) > 72:
        subject = subject[:72].rsplit(" ", 1)[0]
    return subject or "Fix the failing CI checks"


def clean_answer(answer):
    """Keep only known actions and well-formed proposals."""
    proposals = []
    for p in answer.get("proposals") or []:
        if not isinstance(p, dict) or p.get("action") not in ACTIONS:
            continue
        confidence = p.get("confidence") if p.get("confidence") in ("low", "medium", "high") else "low"
        payload = p.get("payload") if isinstance(p.get("payload"), dict) else {}
        reason = str(p.get("reason") or "")[:600]
        if p["action"] == "apply_fix":
            payload["message"] = str(payload.get("message") or "").strip()[:500] or fix_message(reason)
        proposals.append({"action": p["action"], "confidence": confidence,
                          "reason": reason, "payload": payload})
    out = {"summary": str(answer.get("summary") or "").strip()[:2000], "proposals": proposals}
    if isinstance(answer.get("addressed"), int) and isinstance(answer.get("total"), int):
        out["addressed"], out["total"] = answer["addressed"], answer["total"]
    auto = answer.get("auto_approve")
    if isinstance(auto, dict) and isinstance(auto.get("on"), bool):
        out["auto_approve"] = {"on": auto["on"], "why": str(auto.get("why") or "")[:300]}
    return out
