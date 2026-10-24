"""The orchestrator. Everything flows one way: the poll produces snapshots, the diff
produces events, rules turn events into chips, passes, and proposed cards, and the
page renders what this publishes over Server-Sent Events. Page actions come back as
POSTs (snooze, pin, card buttons)."""
import concurrent.futures
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from . import actions, passes, rules, worktrees
from .agents import AgentError, adapter_for, read_text
from .config import CONFIDENCE, HARD_LOCKED, page_config, update_config
from .events import derive
from .github import GitHub, GitHubError
from .poll import Poller
from .state import State, now_iso

POLL_SECONDS = 60
PASS_WORKERS = 2
# Which passes may propose an action the companion presses itself, and on whose PRs.
# Anything else stays a button, even when its trust flag is on.
AUTO_POLICY = {
    "rerun_checks": {"sources": {"ci_failed"}, "mine": True},
    "apply_fix": {"sources": {"ci_failed"}, "mine": True},
    "rerequest_review": {"sources": {"rule"}, "mine": True},
    "enable_auto_merge": {"sources": {"changes_requested", "ci_failed"}, "mine": True},
    "approve": {"sources": {"re_review"}, "mine": False},
}
TRUST_OFFER_AFTER = 3  # manual clicks on one action type before the card offers "do this automatically"
LIVE_CACHE_SECONDS = 15

PASS_ACTIVITY = {
    "changes_requested": "asked for a plan",
    "ci_failed": "asked why CI failed",
    "pre_review": "ran a pre-review",
    "re_review": "asked for a re-check of the review threads",
    "ask": "asked a question",
}


def session_used(mode, pinned_after):
    if mode == "fork":
        return "in a fork of the pinned session"
    if mode == "resume":
        return "in the pinned session"
    return "in a new session, now pinned" if pinned_after else "in a new throwaway session"


class Companion:
    def __init__(self, config, roots, db_path, github=None, poll_seconds=POLL_SECONDS):
        self.config = config
        self.roots = roots
        self.db = State(db_path)
        self.github = github or GitHub()
        self.poller = Poller(self.github)
        self.poll_seconds = poll_seconds
        self.viewer = None
        self.status = {"github": "starting", "lastPoll": None}
        self.worktree_map = {}
        self.live_cache = {}
        self.info_cache = {}
        self.version = 0
        self.cond = threading.Condition()
        self.wake = threading.Event()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=PASS_WORKERS,
                                                          thread_name_prefix="pass")
        self.running = set()
        self.run_lock = threading.Lock()
        self.stopped = False

    # ---------- lifecycle
    @property
    def adapter(self):
        return adapter_for(self.config["agent"])

    def start(self):
        threading.Thread(target=self.poll_loop, name="poll", daemon=True).start()
        self.drain_queue()

    def stop(self):
        self.stopped = True
        self.wake.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def poll_loop(self):
        while not self.stopped:
            try:
                self.poll_once()
            except Exception as e:  # keep polling through any one bad cycle
                self.status["github"] = str(e)
                print(f"Warning: poll failed: {e}")
                if not isinstance(e, GitHubError):
                    traceback.print_exc()
                self.publish()
            self.wake.wait(self.poll_seconds)
            self.wake.clear()

    def poke(self):
        """Poll again soon, e.g. right after an action changed the PR."""
        self.wake.set()

    # ---------- publishing
    def publish(self):
        with self.cond:
            self.version += 1
            self.cond.notify_all()

    def wait_for_change(self, seen, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.version != seen, timeout=timeout)
            return self.version

    # ---------- polling
    def poll_once(self):
        viewer, snaps, listed = self.poller.poll(self.db.open_node_ids())
        self.viewer = viewer
        try:
            found = worktrees.discover(self.roots, None)
            self.worktree_map = {f"{w['repo'] or ''}#{w['branch']}": w for w in found}
        except Exception as e:
            print(f"Warning: worktree discovery failed: {e}")
        for snap in snaps:
            if snap["state"] == "OPEN" and snap["id"] not in listed:
                snap["dropped"] = True  # still open, but no longer yours to act on
                for card in self.db.cards(snap["key"], ("open",)):
                    self.db.update_card(card["id"], status="done")
            self.ingest(snap)
        for snap in self.db.prs():
            if snap.get("state") == "OPEN" and not snap.get("dropped"):
                self.resolve_stale_cards(snap)
                # A running pass's transcript would win the "latest session" fallback.
                if snap["mine"] and not any(p["status"] in ("queued", "running")
                                            for p in self.db.passes(snap["key"])):
                    self.ensure_pin(snap)
        self.db.forget_old_prs()
        self.status = {"github": "ok", "lastPoll": now_iso()}
        self.publish()

    def ingest(self, snap):
        prev = self.db.pr(snap["key"])
        self.db.save_pr(snap)
        for event in derive(prev, snap):
            # Activity holds Shipyard's own actions; the page reads GitHub's timeline itself.
            if not self.db.record_event(snap["key"], event):
                continue
            if snap.get("dropped"):
                continue
            for reaction in rules.reactions(event, snap, self.config["rules"]):
                reaction.setdefault("head", event.get("head"))
                try:
                    self.react(reaction, snap)
                except Exception as e:
                    print(f"Warning: {reaction['do']} on {snap['key']} failed: {e}")

    def react(self, reaction, snap):
        do = reaction["do"]
        key = snap["key"]
        if do == "pin":
            self.ensure_pin(snap)
        elif do == "propose":
            if not any(c.get("action") == reaction["action"] for c in self.db.cards(key, ("open",))):
                card_id = self.db.add_card(key, {
                    "kind": "proposal", "action": reaction["action"], "title": reaction["title"],
                    "summary": reaction["reason"], "payload": reaction["payload"], "head": snap["head"]})
                self.maybe_auto(card_id, reaction["action"], reaction["payload"], "high",
                                reaction["reason"], source="rule")
        elif do == "close_cards":
            for card in self.db.cards(key, ("open",)):
                if not reaction.get("kinds") or card.get("pass") in reaction["kinds"]:
                    self.db.update_card(card["id"], status="done")
        elif do == "review_worktree":
            result = worktrees.review_worktree(snap["repo"], snap["number"], snap["head"], self.roots)
            if result["ok"]:
                pin = self.db.pin(key)
                self.db.set_pin(key, result["path"], pin["session_id"] if pin else None,
                                pin["how"] if pin else "review")
        elif do == "pass":
            self.offer_or_queue(snap, reaction["pass"], reaction["mode"], reaction.get("head"))

    def checkout_for(self, snap):
        """Where a PR's code lives locally: its branch's checkout, or the review worktree."""
        pin = self.db.pin(snap["key"])
        if pin and pin.get("path") and os.path.isdir(pin["path"]):
            return pin["path"]
        wt = self.worktree_map.get(f"{snap['repo']}#{snap['branch']}")
        if wt and snap["mine"]:
            return wt["path"]
        return None

    def ensure_pin(self, snap, force=False):
        key = snap["key"]
        adapter = self.adapter
        wt = self.worktree_map.get(f"{snap['repo']}#{snap['branch']}")
        if not adapter or not wt:
            return None
        current = self.db.pin(key)
        if current and (current.get("session_id") or current.get("how") == "unpinned") and not force:
            return current
        # A main clone holds sessions about many PRs, so its newest one says nothing about this PR.
        found = adapter.find_pr_session(wt["path"], snap["branch"], snap["url"], latest=not wt.get("main"))
        if found:
            self.db.set_pin(key, wt["path"], found["id"], found["how"])
            self.db.log(key, f"pinned {self.config['agent']['name']} session"
                        + (" (latest in the checkout)" if found["how"] == "latest" else ""))
            return self.db.pin(key)
        return None

    def resolve_stale_cards(self, snap):
        for card in self.db.cards(snap["key"], ("open",)):
            if card.get("kind") in ("running",):
                continue
            if rules.card_is_stale(card, snap):
                self.db.update_card(card["id"], status="done")

    # ---------- passes
    def offer_or_queue(self, snap, name, mode, head=None):
        key = snap["key"]
        old = [c for c in self.db.cards(key, ("open",))
               if c.get("pass") == name and c.get("kind") in ("offer", "pass", "raw", "error")]
        if mode == "ask":
            new = self.db.add_card(key, {"kind": "offer", "pass": name, "title": passes.TITLES[name],
                                         "head": snap["head"]})
        else:
            # Already ran for this head (one pass per PR, event, head): keep its card.
            new = self.queue_pass(snap, name, head=head)
        if new:
            for card in old:
                self.db.update_card(card["id"], status="done")
        return new

    def queue_pass(self, snap, name, question=None, retry=False, head=None):
        """Queue a pass with its "running" card; None when it already ran for this head.
        `head` is the dedupe key: the PR's head SHA, or a finer one from the event."""
        key = snap["key"]
        head = None if name == "ask" else head or snap["head"]
        card_id = self.db.add_card(key, {"kind": "running", "pass": name, "title": passes.TITLES[name],
                                         "head": snap["head"], "question": question})
        pass_id = self.db.queue_pass(key, name, head, passes.PASS_MODES[name], card_id, question)
        if pass_id is None:
            existing = next((p for p in self.db.passes(key) if p["name"] == name and p["head"] == head), None)
            if not existing or not retry or existing["status"] not in ("failed", "skipped", "done"):
                self.db.delete_card(card_id)
                return None
            self.db.update_pass(existing["id"], status="queued", error=None, card_id=card_id)
        self.drain_queue()
        self.publish()
        return card_id

    def drain_queue(self):
        for row in self.db.queued_passes():
            if not row.get("card_id") or not self.db.claim_pass(row["id"]):
                continue  # another thread claimed it first
            with self.run_lock:
                self.running.add(row["id"])
            self.pool.submit(self.run_pass_safely, row["id"])

    def run_pass_safely(self, pass_id):
        try:
            self.run_pass(pass_id)
        except Exception as e:
            traceback.print_exc()
            self.fail_pass(self.db.pass_(pass_id), str(e))
        finally:
            with self.run_lock:
                self.running.discard(pass_id)
            self.publish()

    def fail_pass(self, row, error, status="failed", raw=None):
        self.db.update_pass(row["id"], status=status, error=error)
        if row.get("card_id"):
            fields = {"kind": "raw" if raw else "error", "error": error}
            if raw:
                fields["raw"] = raw
            self.db.update_card(row["card_id"], **fields)

    def run_pass(self, pass_id):
        row = self.db.pass_(pass_id)
        snap = self.db.pr(row["key"])
        name = row["name"]
        adapter = self.adapter
        agent = self.config["agent"]
        if not snap:
            return self.fail_pass(row, "Shipyard no longer has this PR.")
        if not adapter:
            return self.fail_pass(row, "No coding agent is configured.")
        if not adapter.available():
            return self.fail_pass(row, f"The {agent['name']} CLI ({adapter.process_name}) isn't on PATH.",
                                  raw=passes.raw_event_text(name, snap))

        mode = passes.PASS_MODES[name]
        question = row.get("question")
        new_session = None
        pin_after = False
        if not snap["mine"]:
            # Someone else's PR is always read at its current head, in the review
            # worktree (made here when a re-review predates the companion).
            made = worktrees.review_worktree(snap["repo"], snap["number"], snap["head"], self.roots)
            if not made["ok"]:
                return self.fail_pass(row, made["error"])
            pin = self.db.pin(snap["key"])
            self.db.set_pin(snap["key"], made["path"], pin["session_id"] if pin else None,
                            pin["how"] if pin else "review")
        if name == "pre_review":
            cwd, session = made["path"], None
            pin_after = True
        else:
            cwd = self.checkout_for(snap)
            if not cwd:
                cwd = worktrees.find_clone(snap["repo"], self.roots)
            pin = self.db.pin(snap["key"])
            if not cwd:
                return self.fail_pass(row, f"No local clone of {snap['repo']} under your roots.",
                                      raw=passes.raw_event_text(name, snap))
            session = pin["session_id"] if pin else None
            if name == "new_session":
                old = adapter.session_file(pin["path"], session) if session else None
                question = str(old) if old else None
                session, pin_after = None, True
            if not session:
                pin_after = pin_after or mode == "resume"
                mode = "fresh"
        if mode == "fork" and not adapter.can_fork:
            mode = "fresh"
            pin_after = False
        if mode == "resume" and adapter.is_live(cwd, session):
            return self.fail_pass(row, f"Your session is open in {agent['name']}, so Shipyard didn't add a "
                                  "turn to it. Paste this in yourself:", status="skipped",
                                  raw=passes.raw_event_text(name, snap))
        if mode == "fresh" and adapter.key == "claude-code":
            new_session = str(uuid.uuid4())

        prompt = passes.render(name, passes.inputs(name, snap, self.github, question))
        try:
            # The PR's own code may carry agent settings (hooks, permissions); on someone
            # else's PR, only yours apply.
            # A session Shipyard keeps gets a title, so it reads the same here and in the agent's app.
            result = adapter.run(cwd, prompt, passes.ANSWER_SCHEMA, mode, session, new_session,
                                 untrusted=not snap["mine"],
                                 name=f'{snap["title"]} (#{snap["number"]})' if pin_after else None)
        except AgentError as e:
            return self.fail_pass(row, str(e))
        answer = passes.clean_answer(result["answer"])
        produced = result.get("session_id")
        throwaway = mode == "fork" or (mode == "fresh" and not pin_after)
        if throwaway and produced and produced != session:
            adapter.discard(cwd, produced)  # the answer is on the card; the transcript isn't needed
        self.db.update_pass(row["id"], status="done", mode=mode,
                            session_id=produced if mode != "fork" else session,
                            summary=answer["summary"][:500])
        if pin_after and produced:
            self.db.set_pin(snap["key"], cwd, produced, "shipyard")
        self.db.log(snap["key"], "started a new session, now pinned" if name == "new_session" else
                    f'{PASS_ACTIVITY.get(name, f"ran {name}")} {session_used(mode, pin_after and produced)}')
        kind = "answer" if name == "ask" else "pass"
        card = self.db.update_card(row["card_id"], kind=kind, summary=answer["summary"],
                                   proposals=answer["proposals"], addressed=answer.get("addressed"),
                                   total=answer.get("total"), mode=mode, session_id=produced,
                                   status="done" if name == "new_session" else None)
        if kind == "pass" and card:
            for proposal in answer["proposals"]:
                if self.maybe_auto(card["id"], proposal["action"], proposal["payload"],
                                   proposal["confidence"], proposal["reason"], source=name):
                    break

    # ---------- trust ladder
    def maybe_auto(self, card_id, action, payload, confidence, reason, source):
        """Press the button for a trusted action type when confidence clears its bar.
        Returns True when it acted; the card moves to Handled with the reason.

        Only on a card that is still open, for a PR still open at the head the card was
        made for, from a pass allowed to propose that action on that PR (AUTO_POLICY)."""
        flag = actions.TRUST_FLAG.get(action)
        rule = self.config["auto"].get(flag) if flag else None
        policy = AUTO_POLICY.get(action)
        if not rule or flag in HARD_LOCKED or not policy or source not in policy["sources"]:
            return False
        if CONFIDENCE.get(confidence, 0) < CONFIDENCE[rule["min_confidence"]]:
            return False
        card = self.db.card(card_id)
        snap = self.db.pr(card["key"]) if card else None
        if (not snap or snap.get("state") != "OPEN" or snap.get("dropped") or
                snap["mine"] != policy["mine"] or card.get("head") != snap.get("head")):
            return False
        if self.db.auto_runs(snap["key"], action) >= rule["max_per_pr"]:
            return False
        if action == "approve":
            payload = {}  # an automatic approval carries no words you haven't read
        if not self.db.claim_card(card_id):
            return False
        try:
            result = actions.run(self.github, action, snap, payload, self.action_context(snap))
        except Exception as e:
            self.db.update_card(card_id, status="open")
            self.db.log(snap["key"], f"tried to {action.replace('_', ' ')} automatically: {e}")
            return False
        self.db.count_auto_run(snap["key"], action)
        self.db.log(snap["key"], f"{result}: {reason}" if reason else result)
        self.db.update_card(card_id, status="handled", handled={
            "action": action, "result": result, "reason": reason, "payload": payload,
            "undoable": action in actions.UNDOABLE, "flag": flag})
        self.poke()
        return True

    def action_context(self, snap):
        wt = self.worktree_map.get(f"{snap['repo']}#{snap['branch']}")
        return {"worktree": wt if snap["mine"] else None}

    # ---------- page requests
    def snapshot_or_error(self, key):
        snap = self.db.pr(key)
        if not snap:
            raise LookupError("Shipyard's companion isn't tracking that PR (it only follows PRs you "
                              "authored, were asked to review, or reviewed).")
        return snap

    def do_action(self, key, action, payload, card_id=None):
        snap = self.snapshot_or_error(key)
        if action == "continue":
            self.db.log(key, "continued in " + self.config["agent"]["name"])
            return {"ok": True}
        result = actions.run(self.github, action, snap, payload, self.action_context(snap))
        self.db.click(action)
        self.db.log(key, result)
        if card_id:
            self.db.update_card(card_id, status="done", result=result)
        self.poke()
        self.publish()
        return {"ok": True, "result": result}

    def card_action(self, card_id, index, payload_override):
        card = self.db.card(card_id)
        if not card or card["status"] != "open":
            raise LookupError("That card is gone or already handled.")
        if card.get("kind") == "proposal":
            action, payload = card["action"], dict(card.get("payload") or {})
        else:
            proposals = card.get("proposals") or []
            if not 0 <= index < len(proposals):
                raise LookupError("That proposal is gone.")
            action, payload = proposals[index]["action"], dict(proposals[index].get("payload") or {})
        payload.update(payload_override or {})
        if not self.db.claim_card(card_id):  # a second press while the first is running
            raise LookupError("That card is already being handled.")
        try:
            return self.do_action(card["key"], action, payload, card_id)
        except Exception:
            self.db.update_card(card_id, status="open")
            raise

    def undo(self, card_id):
        card = self.db.card(card_id)
        handled = (card or {}).get("handled") or {}
        if not handled.get("undoable"):
            raise LookupError("That can't be undone from here.")
        snap = self.snapshot_or_error(card["key"])
        result = actions.undo(self.github, handled["action"], snap, handled.get("payload"))
        self.db.log(card["key"], f"undid: {result}")
        self.db.update_card(card_id, status="done", result=f"undone: {result}")
        self.poke()
        self.publish()
        return {"ok": True, "result": result}

    def dismiss(self, card_id):
        card = self.db.update_card(card_id, status="dismissed")
        if not card:
            raise LookupError("That card is gone.")
        self.publish()
        return {"ok": True}

    def snooze(self, key, hours):
        until = None
        if hours:
            until = (datetime.now(timezone.utc) + timedelta(hours=float(hours))).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.db.set_snooze(key, until)
        self.db.log(key, f"snoozed until {until}" if until else "unsnoozed")
        self.publish()
        return {"ok": True, "until": until}

    def ask(self, key, question):
        snap = self.snapshot_or_error(key)
        question = " ".join((question or "").split())[:1000]
        if not question:
            raise ValueError("Type a question first.")
        for card in self.db.cards(key, ("open",)):
            if card.get("pass") == "ask":
                self.db.update_card(card["id"], status="done")
        return {"ok": True, "card": self.queue_pass(snap, "ask", question)}

    def run_pass_now(self, key, name, node_id=None):
        if name == "new_session" and node_id and self.viewer and not self.db.pr(key):
            # Merged before the companion saw it: read it once so there is something to pin to.
            for found in self.poller.details([node_id], self.viewer):
                self.db.save_pr(found)
        snap = self.snapshot_or_error(key)
        if name not in passes.TITLES or name == "ask":
            raise ValueError(f"Unknown pass: {name}")
        for card in self.db.cards(key, ("open",)):
            if card.get("pass") == name and (card.get("kind") in ("offer", "error", "raw") or name == "new_session"):
                self.db.update_card(card["id"], status="done")
        card = self.queue_pass(snap, name, retry=True)
        if card is None:
            raise ValueError("That pass is already running for this commit.")
        return {"ok": True, "card": card}

    def sessions_for(self, key):
        snap = self.snapshot_or_error(key)
        path = self.checkout_for(snap)
        adapter = self.adapter
        if not path or not adapter:
            return {"ok": True, "sessions": []}
        out = []
        for s in adapter.list_sessions(path)[:15]:
            info = self.session_info(path, s["id"]) or s
            out.append({"id": s["id"], "title": info.get("title") or s["id"], "updated": s["updated"]})
        return {"ok": True, "sessions": out}

    def repin(self, key, session_id):
        snap = self.snapshot_or_error(key)
        path = self.checkout_for(snap)
        if not path:
            raise LookupError("This PR has no local checkout to pin a session in.")
        if session_id not in {s["id"] for s in (self.adapter.list_sessions(path) if self.adapter else [])}:
            raise LookupError("That session isn't in this PR's checkout.")
        self.db.set_pin(key, path, session_id, "manual")
        self.db.log(key, "re-pinned the PR session")
        self.publish()
        return {"ok": True}

    def unpin(self, key):
        self.snapshot_or_error(key)
        pin = self.db.pin(key)
        # Kept as a row so the poll doesn't pin its same guess again.
        self.db.set_pin(key, pin["path"] if pin else None, None, "unpinned")
        self.db.log(key, "unpinned the PR session")
        self.publish()
        return {"ok": True}

    def set_trust(self, flag, on):
        if flag in HARD_LOCKED and on:
            raise ValueError(f"{flag} posts under your name, so it stays manual.")
        self.config = update_config("auto", flag, {"when": "confidence >= high", "max_per_pr": 2} if on else False)
        self.publish()
        return {"ok": True}

    def set_rule(self, name, value):
        self.config = update_config("rules", name, value)
        self.publish()
        return {"ok": True}

    # ---------- the page's view
    def is_live(self, path, session_id):
        now = time.time()
        hit = self.live_cache.get((path, session_id))
        if hit and now - hit[0] < LIVE_CACHE_SECONDS:
            return hit[1]
        live = self.adapter.is_live(path, session_id) if self.adapter else False
        self.live_cache[(path, session_id)] = (now, live)
        return live

    def session_info(self, path, session_id):
        adapter = self.adapter
        f = adapter.session_file(path, session_id) if adapter else None
        if not f:
            return None
        try:
            mtime = os.path.getmtime(f)
        except OSError:
            return None
        cached = self.info_cache.get((path, session_id))
        if not cached or cached[0] != mtime:  # transcripts are re-read only after a write
            text = read_text(f)
            messages = adapter.user_messages(text)
            title = adapter.title(text) or next((m for m in messages if m.strip()), "")
            cached = (mtime, {"id": session_id, "title": " ".join(title.split())[:120],
                              "turns": len(messages), "updated": int(mtime)})
            self.info_cache[(path, session_id)] = cached
        return {**cached[1], "live": self.is_live(path, session_id)}

    def card_view(self, card):
        view = {k: card.get(k) for k in ("id", "status", "kind", "pass", "action", "title", "summary",
                                         "proposals", "payload", "raw", "error", "handled", "created",
                                         "question", "addressed", "total", "result")}
        offers = []
        for p in (card.get("proposals") or []) + ([{"action": card["action"], "payload": card.get("payload")}]
                                                   if card.get("action") else []):
            flag = actions.TRUST_FLAG.get(p["action"])
            if (flag and flag not in HARD_LOCKED and not self.config["auto"].get(flag) and
                    self.db.clicks(p["action"]) >= TRUST_OFFER_AFTER and
                    (p["action"] != "apply_fix" or (p.get("payload") or {}).get("diff"))):
                offers.append(flag)
        view["trustOffers"] = sorted(set(offers))
        return view

    def page_state(self):
        now = datetime.now(timezone.utc)
        prs = {}
        for snap in self.db.prs():
            key = snap["key"]
            pin = self.db.pin(key)
            session = None
            if pin and pin.get("session_id") and pin.get("path"):
                session = self.session_info(pin["path"], pin["session_id"])
                if session:
                    session["shipyardTurns"] = self.db.shipyard_turns(pin["session_id"])
                    session["how"] = pin.get("how")
            cards = [self.card_view(c) for c in self.db.cards(key)]
            # Your own asks and new sessions show in the sidebar where you started them, not as a chip.
            next_cards = [c for c in cards if c["status"] == "open" and c.get("pass") not in ("ask", "new_session")]
            done = [p for p in self.db.passes(key) if p["status"] == "done"]
            exchange = done[-1] if done else None
            path = pin.get("path") if pin else None
            prs[key] = {
                "chip": rules.memory_chip(snap, self.config["rules"], now, self.db.snooze(key),
                                          self.db.last_logged(key, actions.NUDGED)),
                "snoozedUntil": self.db.snooze(key),
                "cards": [c for c in cards if c["status"] == "open"],
                "handled": [c for c in cards if c["status"] == "handled"],
                "planReady": any(c["kind"] in ("pass", "answer") for c in next_cards),
                "running": any(c["kind"] == "running" for c in next_cards),
                "session": session,
                "path": path or self.checkout_for(snap),
                "exchange": ({"pass": exchange["name"], "at": exchange["finished"],
                              "summary": exchange["summary"], "mode": exchange["mode"]} if exchange else None),
                "activity": self.db.activity(key),
            }
        adapter = self.adapter
        return {
            "version": self.version,
            "viewer": self.viewer,
            "status": {**self.status,
                       "agent": ({"name": self.config["agent"]["name"], "available": adapter.available(),
                                  "canFork": adapter.can_fork} if adapter else None)},
            "config": page_config(self.config),
            "prs": prs,
        }
