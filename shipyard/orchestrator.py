"""The orchestrator. Everything flows one way: the poll produces snapshots, the diff
produces events, rules turn events into chips, passes, and proposed cards, and the
page renders what this publishes over Server-Sent Events. Page actions come back as
POSTs (snooze, pin, card buttons)."""
import concurrent.futures
import functools
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from . import actions, passes, rules, worktrees
from .agents import AgentError, adapter_for, process_cwds, read_text
from .config import CONFIDENCE, CONFIG_PATH, HARD_LOCKED, config_mtime, page_config, read_config, read_raw, view_targets
from .events import derive
from .github import GitHub, GitHubError
from .poll import MERGED_SEARCHES, Poller
from .settings import MISSING, SECTIONS, fields as setting_fields, save as save_setting_file
from .state import State, now_iso

POLL_SECONDS = 60
PASS_WORKERS = 2
# Which passes may propose an action the server presses itself, and on whose PRs.
# Anything else stays a button, even when its trust flag is on.
AUTO_POLICY = {
    "rerun_checks": {"sources": {"ci_failed"}, "mine": True},
    "apply_fix": {"sources": {"ci_failed"}, "mine": True},
    "approve": {"sources": {"re_review"}, "mine": False},
}
# How the log names an automatic action it held back, and one it already took.
REVIEW_STATES = {"APPROVE": "APPROVED", "COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}
AUTO_WORDS = {
    "approve": ("the approval", "approved"),
    "rerun_checks": ("the re-run", "re-ran checks on"),
    "apply_fix": ("the fix", "pushed a fix to"),
}
SOURCE_NAMES = {"re_review": "re-review", "ci_failed": "CI assessment", "feedback": "feedback pass"}
LIVE_CACHE_SECONDS = 15
MERGED_CACHE_SECONDS = 300
MERGED_EVERY_SECONDS = 15 * 60

PASS_ACTIVITY = {
    "feedback": "asked for a plan",
    "ci_failed": "asked why CI failed",
    "pre_review": "ran a pre-review",
    "re_review": "asked for a re-check of the review threads",
    "ask": "asked a question",
}


def reply_context(snap, comment_id):
    """Where a reply lands: the file, line and code of the thread holding that comment."""
    if not comment_id:
        return None
    for t in snap.get("threads") or []:
        if any(str(c.get("id")) == str(comment_id) for c in t.get("comments") or []):
            return {"path": t.get("path"), "line": t.get("line"), "hunk": t.get("hunk"),
                    "outdated": t.get("outdated")} if t.get("hunk") else None
    return None


def transcripts_stamp(adapter, path):
    mtimes = []
    for _, f in adapter.session_files(path):
        try:
            mtimes.append(os.path.getmtime(f))
        except OSError:
            continue
    return path, len(mtimes), max(mtimes, default=0)


def session_used(mode, pinned_after, kept=False):
    if kept:
        return "in a fork of your open session, kept" if mode == "fork" else "in a new session, kept (yours was open)"
    if mode == "fork":
        return "in a fork of the pinned session"
    if mode == "resume":
        return "in the pinned session"
    return "in a new session, now pinned" if pinned_after else "in a new throwaway session"


class Orchestrator:
    def __init__(self, config, roots, db_path, github=None, poll_seconds=POLL_SECONDS, config_path=CONFIG_PATH,
                 first_run_hold=False):
        self.config = config
        self.config_path = config_path
        self.config_mtime = config_mtime(config_path)
        # Folders from the command line hold for the run; otherwise the config's apply live.
        self.fixed_roots = worktrees.normalize_roots(roots or [])
        self.db = State(db_path)
        # A new install doesn't act on its own until you say so: passes wait for Ask and
        # nothing is pressed for you. An install that has polled before keeps going.
        # Decided once, at the first start: a held install that has since polled stays held.
        existing = not first_run_hold or self.db.has_history()
        if self.db.automation() is None:
            self.db.set_automation("on" if existing else "held")
        if self.db.welcomed() is None:
            self.db.set_welcomed(existing)
        self.github = github or GitHub()
        self.poller = Poller(self.github)
        self.poll_seconds = poll_seconds
        # The last poll's lists and time, so the board shows right away after a restart.
        memory = self.db.poll_memory() or {}
        self.poller.restore(memory)
        self.viewer = memory.get("viewer") if self.poller.lists else None
        self.status = {"github": "starting", "lastPoll": memory.get("lastPoll") if self.viewer else None}
        self.worktree_map = {}
        self.live_cache = {}
        self.info_cache = {}
        self.merged_cache = {}  # (view, kind, cursor) -> (time, page)
        self.pin_misses = {}  # PR key -> the transcripts a lookup found no session in
        self.recent_merged = {kind: [] for kind in MERGED_SEARCHES}
        self.merged_version = 0  # bumped when recent_merged changes, so the page gets it only then
        self.merged_checked = 0
        self.merged_wanted = False
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
            self.reload_config()
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

    @property
    def roots(self):
        return self.fixed_roots or worktrees.normalize_roots(self.config["roots"])

    def use_config(self, config):
        """Take a new config; a change of folders to scan takes effect at once."""
        before = self.roots
        self.config = config
        if self.roots != before:
            worktrees.warn_unavailable_roots(self.roots)
            self.refresh_worktrees()
        self.publish()

    def reload_config(self):
        """Pick up a hand edit to the config without a restart, which would cut running
        passes short."""
        mtime = config_mtime(self.config_path)
        if mtime == self.config_mtime:
            return
        self.config_mtime = mtime
        try:
            config = read_config(self.config_path)
        except ValueError as e:
            print(f"Warning: kept the previous config: {e}")
            return
        self.use_config(config)

    def poke(self):
        """Poll again soon, e.g. right after an action changed the PR."""
        self.wake.set()

    def held(self):
        return self.db.automation() != "on"

    def dismiss_welcome(self):
        self.db.set_welcomed()
        self.publish()
        return {"ok": True}

    def setup_checks(self):
        """Whether GitHub and the coding agent work from here, for the welcome."""
        checks = []
        try:
            login = self.github.rest("GET", "/user").get("login")
            checks.append({"id": "github", "label": "GitHub", "ok": True,
                           "detail": f"Signed in as {login} with the GitHub CLI" if login else "Signed in with the GitHub CLI"})
            if self.status["github"] != "ok":
                self.wake.set()
        except GitHubError as e:
            checks.append({"id": "github", "label": "GitHub", "ok": False, "detail": str(e),
                           "fix": "Run `gh auth login` in a terminal, then check again."})
        agent, adapter = self.config["agent"], self.adapter
        if not adapter:
            checks.append({"id": "agent", "label": "Coding agent", "ok": True,
                           "detail": "None set, so passes stay off. Pick one in Settings."})
        else:
            try:
                checks.append({"id": "agent", "label": agent["name"], "ok": True,
                               "detail": f"`{adapter.process_name}` runs: {adapter.version()}"})
            except AgentError as e:
                checks.append({"id": "agent", "label": agent["name"], "ok": False, "detail": str(e),
                               "fix": f"Install {agent['name']} so `{adapter.process_name}` is on the PATH Shipyard runs with"})
        return {"ok": True, "checks": checks}

    def turn_on_automation(self):
        self.db.set_automation("on")
        self.publish()
        return {"ok": True}

    def check_now(self, merged=False):
        """Poll now: the page was refreshed or came back into view. `merged` also searches
        merged history again, which the poll otherwise does every MERGED_EVERY_SECONDS."""
        self.merged_wanted = self.merged_wanted or merged
        self.poke()
        return {"ok": True}

    def refresh_merged(self):
        if not self.merged_wanted and time.time() - self.merged_checked < MERGED_EVERY_SECONDS:
            return
        try:
            recent = {kind: self.poller.recent_merged(kind) for kind in MERGED_SEARCHES}
        except Exception as e:  # the next poll tries again; open PRs matter more
            print(f"Warning: merged history search failed: {e}")
            return
        self.merged_checked, self.merged_wanted = time.time(), False
        if recent != self.recent_merged:
            self.recent_merged = recent
            self.merged_version += 1

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
    def refresh_worktrees(self):
        try:
            found = worktrees.discover(self.roots, None)
            self.worktree_map = {f"{w['repo'] or ''}#{w['branch']}": w for w in found}
        except Exception as e:
            print(f"Warning: worktree discovery failed: {e}")

    def rescan_worktrees(self):
        """Look for new or changed checkouts now, e.g. after a branch action or when the tab regains focus."""
        self.refresh_worktrees()
        self.publish()
        return {"ok": True}

    def poll_once(self):
        self.refresh_worktrees()  # local, so it stays current while GitHub is unreachable
        viewer, snaps, listed = self.poller.poll(self.db.open_node_ids())
        self.viewer = viewer
        for snap in snaps:
            if snap["state"] == "OPEN" and snap["id"] not in listed:
                snap["dropped"] = True  # still open, but no longer yours to act on
                for card in self.db.cards(snap["key"], ("open",)):
                    self.db.update_card(card["id"], status="done")
            self.ingest(snap)
        for snap in self.db.prs():
            if snap.get("state") == "MERGED" or snap.get("state") == "OPEN" and not snap.get("dropped"):
                self.resolve_stale_cards(snap)
            if snap.get("state") == "OPEN" and not snap.get("dropped"):
                # A running pass's transcript would win the "latest session" fallback.
                if snap["mine"] and not any(p["status"] in ("queued", "running")
                                            for p in self.db.passes(snap["key"])):
                    self.ensure_pin(snap)
        self.refresh_merged()
        self.retry_waiting_actions()
        self.db.prune()
        self.status = {"github": "ok", "lastPoll": now_iso()}
        self.db.set_poll_memory({**self.poller.memory(), "viewer": viewer, "lastPoll": self.status["lastPoll"]})
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
                if ((not reaction.get("kinds") or card.get("pass") in reaction["kinds"]) and
                        card.get("pass") not in reaction.get("except", ())):
                    self.db.update_card(card["id"], status="done")
        elif do == "review_worktree":
            result = worktrees.review_worktree(snap["repo"], snap["number"], snap["head"], self.roots)
            if result["ok"]:
                pin = self.db.pin(key)
                self.db.set_pin(key, result["path"], pin["session_id"] if pin else None,
                                pin["how"] if pin else "review")
        elif do == "pass":
            mode = "ask" if self.held() and reaction["mode"] == "auto" else reaction["mode"]
            self.offer_or_queue(snap, reaction["pass"], mode, reaction.get("head"))

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
        # A lookup reads every transcript in the checkout, so a miss stands until they change.
        stamp = transcripts_stamp(adapter, wt["path"])
        if not force and self.pin_misses.get(key) == stamp:
            return None
        # A main clone holds sessions about many PRs, so its newest one says nothing about this PR.
        found = adapter.find_pr_session(wt["path"], snap["branch"], snap["url"], latest=not wt.get("main"))
        if found:
            self.pin_misses.pop(key, None)
            self.db.set_pin(key, wt["path"], found["id"], found["how"])
            self.db.log(key, f"pinned {self.config['agent']['name']} session"
                        + (" (latest in the checkout)" if found["how"] == "latest" else ""))
            return self.db.pin(key)
        self.pin_misses[key] = stamp
        return None

    def resolve_stale_cards(self, snap):
        for card in self.db.cards(snap["key"], ("open",)):
            if card.get("kind") in ("running",):
                continue
            if rules.card_is_stale(card, snap, self.config["rules"]):
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
        review_path = None
        if not snap["mine"]:
            # Someone else's PR is always read at its current head, in the review
            # worktree (made here when a re-review predates the server).
            made = worktrees.review_worktree(snap["repo"], snap["number"], snap["head"], self.roots)
            if not made["ok"]:
                return self.fail_pass(row, made["error"])
            review_path = made["path"]
            pin = self.db.pin(snap["key"])
            self.db.set_pin(snap["key"], review_path, pin["session_id"] if pin else None,
                            pin["how"] if pin else "review")
        if name == "pre_review":
            if not review_path:
                return self.fail_pass(row, "A pre-review reads someone else's PR, and this one is yours.")
            cwd, session = review_path, None
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
        kept = False
        if mode == "resume" and adapter.is_live(cwd, session):
            mode, kept = "fork" if adapter.can_fork else "fresh", True
        if mode == "fresh" and adapter.key == "claude-code":
            new_session = str(uuid.uuid4())

        prompt = passes.render(name, passes.inputs(name, snap, self.github, question))
        try:
            # The PR's own code may carry agent settings (hooks, permissions); on someone
            # else's PR, only yours apply.
            # A session Shipyard keeps gets a title, so it reads the same here and in the agent's app.
            result = adapter.run(cwd, prompt, passes.ANSWER_SCHEMA, mode, session, new_session,
                                 untrusted=not snap["mine"],
                                 name=f'{snap["title"]} (#{snap["number"]})' if pin_after else
                                 f'{passes.TITLES[name]}: {snap["title"]} (#{snap["number"]})' if kept else None)
        except AgentError as e:
            return self.fail_pass(row, str(e))
        answer = passes.clean_answer(result["answer"])
        produced = result.get("session_id")
        throwaway = not kept and (mode == "fork" or (mode == "fresh" and not pin_after))
        if throwaway and produced and produced != session:
            adapter.discard(cwd, produced)  # the answer is on the card; the transcript isn't needed
        self.db.update_pass(row["id"], status="done", mode=mode,
                            session_id=produced if mode != "fork" else session,
                            summary=answer["summary"][:500])
        if pin_after and produced:
            self.db.set_pin(snap["key"], cwd, produced, "shipyard")
        self.db.log(snap["key"], "started a new session, now pinned" if name == "new_session" else
                    f'{PASS_ACTIVITY.get(name, f"ran {name}")} {session_used(mode, pin_after and produced, kept)}')
        if name == "pre_review" and answer.get("auto_approve"):
            saved = self.db.auto_approve(snap["key"])
            if not saved or saved["by"] == "review":
                auto = answer["auto_approve"]
                self.db.set_auto_approve(snap["key"], auto["on"], "review", auto["why"])
        if name in ("pre_review", "re_review"):
            answer["proposals"] = self.draft_on_github(snap, answer["proposals"])
        kind = "answer" if name == "ask" else "pass"
        for proposal in answer["proposals"]:
            context = reply_context(snap, (proposal.get("payload") or {}).get("comment_id"))
            if proposal["action"] == "post_reply" and context:
                proposal["context"] = context
        card = self.db.update_card(row["card_id"], kind=kind, summary=answer["summary"],
                                   proposals=answer["proposals"], addressed=answer.get("addressed"),
                                   total=answer.get("total"), mode=mode, session_id=produced,
                                   kept_path=cwd if kept else None,
                                   status="done" if name == "new_session" else None)
        if kind == "pass" and card:
            acted = False
            for proposal in answer["proposals"]:
                if self.maybe_auto(card["id"], proposal["action"], proposal["payload"],
                                   proposal["confidence"], proposal["reason"], source=name):
                    acted = True
                    break
            if name == "re_review" and not acted:
                self.note_unapproved(card["id"], snap, answer["proposals"])

    def draft_on_github(self, snap, proposals):
        """An approval with no comments stays a button, whatever its confidence; any other
        review is drafted as a pending review, so it is edited and submitted on GitHub
        next to the diff."""
        out = []
        drafted = False
        for p in proposals:
            payload = p["payload"]
            if p["action"] == "comment_unresolved":
                payload = {"event": "COMMENT", "body": payload.get("body") or ""}
            elif p["action"] != "submit_review":
                out.append(p)
                continue
            elif payload.get("event") == "APPROVE" and not payload.get("comments"):
                out.append({**p, "action": "approve", "payload": {"body": payload.get("body") or ""}})
                continue
            try:
                result = actions.run(self.github, "draft_review", snap, payload)
                self.db.log(snap["key"], result)
                error = None
                drafted = True
            except Exception as e:
                error = str(e)
            out.append({**p, "action": "finish_on_github",
                        "payload": {"event": payload.get("event"), "error": error,
                                    "comments": len(payload.get("comments") or [])}})
        # GitHub allows one pending review, so an Approve button would fail; approve it there.
        return [p for p in out if p["action"] != "approve"] if drafted else out

    # ---------- trust ladder
    def maybe_auto(self, card_id, action, payload, confidence, reason, source, checks=None):
        """Press the button for a trusted action type when confidence clears its bar.
        Returns True when it acted; the card moves to Handled with the reason.

        Only on a card that is still open, for a PR still open at the head the card was
        made for, from a pass allowed to propose that action on that PR (AUTO_POLICY)."""
        flag = actions.TRUST_FLAG.get(action)
        rule = self.config["auto"].get(flag) if flag and not self.held() else None
        policy = AUTO_POLICY.get(action)
        if not rule or flag in HARD_LOCKED or not policy or source not in policy["sources"]:
            return False
        card = self.db.card(card_id)
        snap = self.db.pr(card["key"]) if card else None
        if not snap or snap.get("state") != "OPEN" or snap.get("dropped") or snap["mine"] != policy["mine"]:
            return False
        if card.get("head") != snap.get("head"):
            if action == "approve":
                self.db.update_card(card_id, auto_held="new commits landed after the re-review read it")
            return False
        if action == "approve" and not self.auto_approve_setting(snap)["on"]:
            self.db.log(snap["key"], "left the approval for you: auto-approve is off for this PR")
            return False
        task, done = AUTO_WORDS.get(action, (action.replace("_", " "), action.replace("_", " ")))
        failing = sorted(c["name"] for c in snap.get("failing") or [] if c.get("name"))
        held = None
        if CONFIDENCE.get(confidence, 0) < CONFIDENCE[rule["min_confidence"]]:
            held = (f"the {SOURCE_NAMES.get(source, 'pass')}'s confidence was {confidence} "
                    f"(needs {rule['min_confidence']})")
        elif self.db.auto_runs(snap["key"], action) >= rule["max_per_pr"]:
            held = f"it already {done} this PR automatically {rule['max_per_pr']} times"
        elif action == "approve":
            held = self.rereview_too_big(snap, rule["max_lines"])
        elif action == "apply_fix":
            held = self.fix_too_big(snap, (payload or {}).get("diff"), rule["max_lines"])
        elif action == "rerun_checks" and checks is not None and not set(failing) <= set(checks):
            held = "more checks failed while it waited"
        if held:
            self.db.log(snap["key"], f"left {task} for you: {held}")
            self.db.update_card(card_id, auto_held=held, auto_action=action)
            return False
        if action == "rerun_checks" and snap.get("checks_running"):
            self.db.update_card(card_id, auto_wait={
                "checks": failing, "action": action, "confidence": confidence, "reason": reason, "source": source})
            return False
        if action == "approve":
            ready_at = self.approve_ready_at(snap, rule["min_wait_minutes"])
            if ready_at > datetime.now(timezone.utc):
                self.db.update_card(card_id, auto_wait={
                    "until": ready_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "action": action,
                    "confidence": confidence, "reason": reason, "source": source})
                return False
            payload = {}  # an automatic approval carries no words you haven't read
        if not self.db.claim_card(card_id):
            return False
        try:
            result = actions.run(self.github, action, snap, payload, self.action_context(snap))
        except Exception as e:
            self.db.update_card(card_id, status="open")
            self.db.log(snap["key"], f"tried to {action.replace('_', ' ')} automatically: {e}")
            return False
        self.record_review(snap, action, payload)
        self.record_pending(snap, action, payload)
        self.db.count_auto_run(snap["key"], action)
        self.db.log(snap["key"], f"{result} automatically: {reason}" if reason else f"{result} automatically")
        self.db.update_card(card_id, status="handled", handled={
            "action": action, "result": result, "reason": reason, "payload": payload,
            "undoable": action in actions.UNDOABLE, "flag": flag})
        self.poke()
        return True

    def approve_ready_at(self, snap, minutes):
        seen = self.db.head_seen_at(snap["key"], snap.get("head")) or now_iso()
        return datetime.fromisoformat(seen.replace("Z", "+00:00")) + timedelta(minutes=minutes)

    def retry_waiting_actions(self):
        """Approvals wait out the time after a push; re-runs wait for running checks."""
        now = now_iso()
        for card in self.db.cards(statuses=("open",)):
            wait = card.get("auto_wait")
            if wait and (wait.get("until") or "") <= now:
                self.db.update_card(card["id"], auto_wait=None)
                self.maybe_auto(card["id"], wait["action"], {}, wait["confidence"],
                                wait["reason"], wait["source"], checks=wait.get("checks"))

    def note_unapproved(self, card_id, snap, proposals):
        """With auto-approve on, a re-review that proposed no approval says so on its card."""
        if self.held() or not self.config["auto"].get("approve_rereview") or not self.auto_approve_setting(snap)["on"]:
            return
        if any(p["action"] == "approve" for p in proposals):
            return
        held = ("the re-review drafted comments for you to finish on GitHub"
                if any(p["action"] == "finish_on_github" for p in proposals)
                else "the re-review didn't recommend approving")
        self.db.update_card(card_id, auto_held=held)

    def auto_approve_setting(self, snap):
        """Whether a re-review may approve this PR on its own: your choice, else the
        pre-review's, else on only when your last review approved it."""
        saved = self.db.auto_approve(snap["key"])
        if saved:
            return saved
        review = snap.get("my_review") or {}
        approved = review.get("state") == "APPROVED" or review.get("dismissed_from") == "APPROVED"
        return {"on": approved, "by": "default", "why": None, "at": None}

    def set_auto_approve(self, key, on):
        snap = self.snapshot_or_error(key)
        if snap["mine"]:
            raise ValueError("Auto-approve is for re-reviews of someone else's PR.")
        self.db.set_auto_approve(key, bool(on), "you")
        self.db.log(key, f"auto-approve on re-reviews: {'on' if on else 'off'} (your choice)")
        self.publish()
        return {"ok": True}

    def rereview_too_big(self, snap, max_lines):
        """Why the change since your last review is too big to approve automatically, or None."""
        reviewed = (snap.get("my_review") or {}).get("commit")
        if not reviewed or not snap.get("head"):
            return "couldn't tell which commit you last reviewed"
        try:
            compare = self.github.rest("GET", f"/repos/{snap['repo']}/compare/{reviewed}...{snap['head']}")
        except Exception as e:
            return f"couldn't size the change since your review ({e})"
        files = compare.get("files") if isinstance(compare, dict) else None
        if files is None or len(files) >= 300:
            return "couldn't size the change since your review"
        added = [f["filename"] for f in files if f.get("status") == "added"]
        if added:
            return f"it adds {', '.join(added[:3])}"
        lines = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
        if lines > max_lines:
            return f"{lines} lines changed since your review (limit {max_lines})"
        return None

    def fix_too_big(self, snap, diff, max_lines):
        """Why a proposed CI fix can't be pushed automatically, or None. It may only edit
        files the PR already changes, and only a few lines of them."""
        paths, added, lines, new_file = set(), [], 0, False
        for line in (diff or "").splitlines():
            if line.startswith(("--- ", "+++ ")):
                path = line[4:].strip()
                if path == "/dev/null":
                    new_file = line.startswith("--- ")
                    continue
                path = path[2:] if path[:2] in ("a/", "b/") else path
                paths.add(path)
                if line.startswith("+++ ") and new_file:
                    added.append(path)
                    new_file = False
            elif line.startswith(("+", "-")):
                lines += 1
        if added:
            return f"it adds {', '.join(added[:3])}"
        if lines > max_lines:
            return f"the fix changes {lines} lines (limit {max_lines})"
        if (snap.get("changed_files") or 0) > 100:
            return "the PR changes too many files to check the fix against"
        try:
            files = self.github.rest("GET", f"/repos/{snap['repo']}/pulls/{snap['number']}/files?per_page=100")
        except Exception as e:
            return f"couldn't list the PR's files ({e})"
        outside = sorted(paths - {f.get("filename") for f in files or []})
        if outside:
            return f"it edits {', '.join(outside[:3])}, which the PR doesn't change"
        return None

    def action_context(self, snap):
        wt = self.worktree_map.get(f"{snap['repo']}#{snap['branch']}")
        return {"worktree": wt if snap["mine"] else None, "roots": self.roots}

    # ---------- page requests
    def snapshot_or_error(self, key):
        snap = self.db.pr(key)
        if not snap:
            raise LookupError("Shipyard isn't tracking that PR (it only follows PRs you "
                              "authored, were asked to review, or reviewed).")
        return snap

    def do_action(self, key, action, payload, card_id=None):
        snap = self.snapshot_or_error(key)
        if action == "continue":
            self.db.log(key, "continued in " + self.config["agent"]["name"])
            return {"ok": True}
        result = actions.run(self.github, action, snap, payload, self.action_context(snap))
        if action == "rerequest_review":
            # Next steps come from the snapshot, so record the request before the poll sees it.
            requested = snap.get("requested") or []
            snap["requested"] = requested + [r for r in payload.get("reviewers") or [] if r not in requested]
            self.db.save_pr(snap)
        self.record_review(snap, action, payload)
        self.record_pending(snap, action, payload)
        self.reread_for_page(snap)
        self.db.log(key, result)
        if card_id:
            self.db.update_card(card_id, status="done", result=result)
        self.poke()
        self.publish()
        return {"ok": True, "result": result}

    def record_pending(self, snap, action, payload):
        """What an action just changed on GitHub, for the page's steps until the poll re-reads
        the PR. It stays out of the fields events diff, so the poll still sees the change."""
        ci = {"ci": "PENDING", "failing": []}
        pending = {"apply_fix": ci, "rerun_checks": ci, "enable_auto_merge": {"auto_merge": True},
                   "undo_enable_auto_merge": {"auto_merge": False}, "disable_auto_merge": {"auto_merge": False},
                   "merge": {"state": "MERGED"},
                   "mark_ready": {"draft": False}}.get(action)
        if action == "post_reply":
            comment = {"id": None, "author": self.viewer, "created": now_iso()}
            reply_to = payload.get("comment_id")
            if reply_to:
                pending = {"threads": [
                    {**t, "comments": t["comments"] + [comment]}
                    if any(str(c.get("id")) == str(reply_to) for c in t.get("comments") or []) else t
                    for t in snap.get("threads") or []]}
            else:
                pending = {"comments": (snap.get("comments") or []) + [comment]}
        if not pending:
            return
        snap["pending"] = {**(snap.get("pending") or {}), **pending}
        self.db.save_pr(snap)
        if action == "apply_fix":
            self.refresh_worktrees()  # the fix may have created the branch's worktree

    def reread_for_page(self, snap):
        """Show the page what an action changed now. Search can lag behind an action, so
        the poll's signature check may not re-read the PR for a while."""
        try:
            fresh = self.poller.details([snap["id"]], self.viewer)
        except Exception as e:  # the action itself already went through
            print(f"Warning: re-reading {snap['key']} after an action failed: {e}")
            return
        if fresh:
            snap["page"] = fresh[0]["page"]
            self.db.save_pr(snap)

    def timeline(self, node_id):
        return {"ok": True, "node": self.poller.timeline(node_id)}

    def views(self):
        """Your views from the config file, and the one showing: "" is everything you're
        involved in, which needs no view at all."""
        views = self.config["views"]
        current = self.db.current_view()
        return {"views": views, "current": current if current in views else ""}

    def folder_candidates(self):
        pr_repos = {snap["repo"] for snap in self.db.prs()}
        return {"ok": True, "candidates": worktrees.folder_candidates(pr_repos, exclude=self.roots)}

    def settings(self):
        return {"ok": True, "sections": SECTIONS, "fields": setting_fields(read_raw(self.config_path)),
                "commandLineRoots": self.fixed_roots}

    def save_setting(self, path, value=None, reset=False, expect=MISSING):
        config, fields = save_setting_file(path, value, reset, expect, self.config_path)
        self.config_mtime = config_mtime(self.config_path)
        self.use_config(config)
        return {"ok": True, "fields": fields}

    def set_current_view(self, view):
        self.db.set_current_view(", ".join(view_targets(view)) if view else "")
        self.publish()
        return {"ok": True, **self.views()}

    def move_old_views(self):
        """Views the page saved in the database before they lived in the config file."""
        old = self.db.take_old_views()
        if not old:
            return
        if old.get("views") and not self.config["views"]:
            self.save_setting(["views"], old["views"])
        self.db.set_current_view(old.get("current") or "")

    def merged_history(self, view, kind, after=None):
        """A page of a view's older merges, for Show more; kept a few minutes so other tabs
        reuse it."""
        key = (", ".join(view_targets(view)) if view else "", kind, after or "")
        now = time.time()
        hit = self.merged_cache.get(key)
        if hit and now - hit[0] < MERGED_CACHE_SECONDS:
            page = hit[1]
        else:
            page = self.poller.merged(view, kind, after)
            self.merged_cache = {k: v for k, v in self.merged_cache.items() if now - v[0] < MERGED_CACHE_SECONDS}
            self.merged_cache[key] = (now, page)
        return {"ok": True, **page, "owners": dict(self.poller.owner_types)}

    def record_review(self, snap, action, payload):
        """Save a review you just submitted, so the card doesn't fall back to "Review" until the poll sees it."""
        if action == "submit_review":
            state = REVIEW_STATES.get(payload.get("event"), "COMMENTED")
        else:
            state = {"approve": "APPROVED", "comment_unresolved": "COMMENTED"}.get(action)
        if not state:
            return
        snap["my_review"] = {"id": None, "state": state, "dismissed_from": None,
                             "submitted": now_iso(), "commit": snap.get("head")}
        snap["requested"] = [r for r in snap.get("requested") or [] if r != self.viewer]
        snap["requested_me"] = False
        self.db.save_pr(snap)

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
        self.record_pending(snap, f"undo_{handled['action']}", handled.get("payload") or {})
        self.reread_for_page(snap)
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
            # Merged before the server saw it: read it once so there is something to pin to.
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
        cwds = self.shared_process_scan()
        for s in adapter.list_sessions(path)[:15]:
            info = self.session_info(path, s["id"], cwds) or s
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

    # ---------- the page's view
    def shared_process_scan(self):
        """The agent's process directories, scanned at most once for every session checked
        in one build, and only if some session's cached answer has expired."""
        adapter = self.adapter
        return functools.cache(lambda: process_cwds(adapter.process_name)) if adapter else None

    def is_live(self, path, session_id, cwds=None):
        now = time.time()
        hit = self.live_cache.get((path, session_id))
        if hit and now - hit[0] < LIVE_CACHE_SECONDS:
            return hit[1]
        live = self.adapter.is_live(path, session_id, cwds) if self.adapter else False
        self.live_cache[(path, session_id)] = (now, live)
        return live

    def session_info(self, path, session_id, cwds=None):
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
        return {**cached[1], "live": self.is_live(path, session_id, cwds)}

    def card_view(self, card):
        view = {k: card.get(k) for k in ("id", "status", "kind", "pass", "action", "title", "summary",
                                         "proposals", "payload", "raw", "error", "handled", "created",
                                         "question", "addressed", "total", "result", "auto_held", "auto_action", "auto_wait")}
        view["kept"] = ({"path": card["kept_path"], "session": card.get("session_id"), "fork": card.get("mode") == "fork"}
                        if card.get("kept_path") else None)
        return view

    def page_state(self):
        now = datetime.now(timezone.utc)
        cwds = self.shared_process_scan()
        prs = {}
        for snap in self.db.prs():
            key = snap["key"]
            view = {**snap, **(snap.get("pending") or {})}
            pin = self.db.pin(key)
            session = None
            if pin and pin.get("session_id") and pin.get("path"):
                session = self.session_info(pin["path"], pin["session_id"], cwds)
                if session:
                    session["shipyardTurns"] = self.db.shipyard_turns(pin["session_id"])
                    session["how"] = pin.get("how")
            cards = [self.card_view(c) for c in self.db.cards(key)]
            open_cards = [c for c in cards if c["status"] == "open"]
            # Your own asks and new sessions show in the sidebar where you started them, not as a chip.
            next_cards = [c for c in cards if c["status"] == "open" and c.get("pass") not in ("ask", "new_session")]
            done = [p for p in self.db.passes(key) if p["status"] == "done"]
            exchange = done[-1] if done else None
            path = pin.get("path") if pin else None
            auto = None if snap["mine"] else self.auto_approve_setting(snap)
            prs[key] = {
                "chip": rules.memory_chip(view, self.config["rules"], now, self.db.snooze(key),
                                          self.db.last_logged(key, actions.NUDGED)),
                "steps": rules.next_steps(view, self.config["rules"], open_cards),
                # What the server last saw, so the page can tell which of them is behind.
                "updated": snap.get("updated"), "head": snap.get("head"), "ci": snap.get("ci"),
                "state": snap.get("state"), "dropped": bool(snap.get("dropped")),
                "openThreads": sum(1 for t in snap.get("threads") or [] if not t.get("resolved")),
                "snoozedUntil": self.db.snooze(key),
                "cards": open_cards,
                "handled": [c for c in cards if c["status"] == "handled"],
                "planReady": any(c["kind"] in ("pass", "answer") for c in next_cards),
                "running": any(c["kind"] == "running" for c in next_cards),
                "session": session,
                "path": path or self.checkout_for(snap),
                "exchange": ({"pass": exchange["name"], "at": exchange["finished"],
                              "summary": exchange["summary"], "mode": exchange["mode"]} if exchange else None),
                "activity": self.db.activity(key),
                "autoApprove": auto if auto and (auto["by"] != "default" or snap.get("my_review")) else None,
                "pr": snap.get("page"),
                "lists": self.poller.lists.get(snap["id"], []),
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
            "worktrees": [w for w in self.worktree_map.values() if w.get("branch")],
            "views": self.views(),
            "automation": "held" if self.held() else "on",
            "welcome": not self.db.welcomed(),
            "roots": self.roots,
        }
