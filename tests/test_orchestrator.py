import copy
import os
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shipyard.agents import AgentError
from shipyard.github import GitHubError
from shipyard.orchestrator import Orchestrator
from shipyard.config import read_config, validate_config
from shipyard.settings import MISSING
from shipyard.state import State

from fixtures import VIEWER, check_run, pr_node


class FakeGitHub:
    def __init__(self):
        self.lists = {"author:@me": [], "review-requested:@me": [], "reviewed-by:@me": []}
        self.nodes = {}
        self.calls = []
        self.compare = {"files": [{"filename": "a.py", "status": "modified", "additions": 3, "deletions": 1}]}
        self.pr_files = [{"filename": "a.py"}]
        self.running_runs = set()

    def put(self, node, *lists):
        self.nodes[node["id"]] = node
        for name in self.lists:
            self.lists[name] = [n for n in self.lists[name] if n["id"] != node["id"]]
        for name in lists:
            self.lists[name].append(node)

    def graphql(self, query, variables=None):
        if "search(" in query:
            nodes = self.lists[max((k for k in self.lists if k in variables["q"]), key=len)]
            return {"viewer": {"login": VIEWER},
                    "search": {"pageInfo": {"hasNextPage": False}, "nodes": copy.deepcopy(nodes)}}
        if "nodes(ids" in query:
            return {"nodes": [copy.deepcopy(self.nodes[i]) for i in variables["ids"]]}
        self.calls.append(("graphql", query.split("(")[0].split()[-1], variables))
        return {}

    def rest(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/rerun-failed-jobs") and int(path.split("/")[-2]) in self.running_runs:
            raise GitHubError('403 {"message":"This workflow is already running"}', 403)
        if "/compare/" in path:
            return self.compare
        if path == "/user":
            return {"login": VIEWER}
        return self.pr_files if path.split("?")[0].endswith("/files") else {}

    def job_log_tail(self, repo, job_id, lines=150):
        return "Error: connect ETIMEDOUT registry.npmjs.org"


class FakeAgent:
    key = "fake"
    process_name = "fake"
    can_fork = True

    def __init__(self, checkout):
        self.checkout = checkout
        self.runs = []
        self.live = False
        self.answers = {}
        self.discarded = []
        self.before_answer = None

    def available(self):
        return True

    def version(self):
        return "1.0.0 (Fake)"

    def find_pr_session(self, path, branch, url, latest=True):
        return {"id": "s-open", "how": "pr-url"}

    def list_sessions(self, path):
        return [{"id": "s-open", "updated": 1}, {"id": "s-other", "updated": 0}]

    def session_file(self, path, sid):
        return None

    def session_files(self, path):
        return []

    def user_messages(self, text):
        return []

    def is_live(self, path, sid, cwds=None):
        return self.live

    def discard(self, path, sid):
        self.discarded.append(sid)
        return True

    def run(self, cwd, prompt, schema, mode="fresh", session_id=None, new_session_id=None, untrusted=False,
            name=None):
        self.runs.append({"cwd": cwd, "prompt": prompt, "mode": mode, "session": session_id,
                          "untrusted": untrusted, "name": name})
        if self.before_answer:
            self.before_answer()
        produced = new_session_id or {"fork": "fork-1", "fresh": "fresh-1"}.get(mode, session_id)
        answer = next((a for marker, a in self.answers.items() if marker in prompt),
                      {"summary": "ok", "proposals": []})
        return {"session_id": produced, "answer": answer}


class OrchestratorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.checkout = str(base / "checkout")
        Path(self.checkout).mkdir()
        self.config_path = base / "shipyard.config.json"
        self.gh = FakeGitHub()
        self.agent = FakeAgent(self.checkout)
        patches = [
            mock.patch("shipyard.orchestrator.adapter_for", return_value=self.agent),
            mock.patch("shipyard.orchestrator.worktrees.discover", return_value=[
                {"repo": "o/r", "branch": "feature", "path": self.checkout, "main": False}]),
            mock.patch("shipyard.orchestrator.worktrees.review_worktree",
                       return_value={"ok": True, "path": self.checkout}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.raw = {}
        self.db_path = base / "shipyard.db"
        self.comp = Orchestrator(validate_config({}), [str(base)], self.db_path, github=self.gh,
                              config_path=self.config_path)
        self.addCleanup(self.comp.db.close)
        self.addCleanup(self.comp.stop)

    def trust(self, flag, value=True):
        self.raw.setdefault("auto", {})[flag] = value
        self.comp.config = validate_config(self.raw)

    def poll(self):
        self.comp.poll_once()
        self.idle()
        return self.comp.page_state()["prs"]

    def idle(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            busy = [p for s in self.comp.db.prs() for p in self.comp.db.passes(s["key"])
                    if p["status"] in ("queued", "running")]
            if not busy and not self.comp.running:
                return
            time.sleep(0.02)
        self.fail("passes did not finish")


class Restart(OrchestratorCase):
    def test_restores_the_last_poll_and_skips_unchanged_prs(self):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        self.comp.db.close()
        again = Orchestrator(validate_config({}), [self.checkout], self.db_path, github=self.gh)
        self.addCleanup(again.db.close)
        state = again.page_state()
        self.assertEqual((state["viewer"], state["prs"]["o/r#1"]["lists"]), (VIEWER, ["mine"]))
        self.assertTrue(state["status"]["lastPoll"])
        with mock.patch.object(again.poller, "details", wraps=again.poller.details) as details:
            again.poll_once()
        self.assertEqual(details.call_args.args[0], [])


class SessionToken(OrchestratorCase):
    def test_outlives_a_restart(self):
        token = self.comp.db.session_token()
        self.comp.db.close()
        self.assertEqual(State(self.db_path).session_token(), token)


class SharedCosts(OrchestratorCase):
    def test_one_process_scan_per_state_build(self):
        with mock.patch("shipyard.orchestrator.process_cwds", return_value=set()) as scan:
            cwds = self.comp.shared_process_scan()
            cwds(), cwds()
        self.assertEqual(scan.call_count, 1)

    def test_a_missed_session_lookup_waits_for_new_transcripts(self):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        self.comp.db.set_pin("o/r#1", None, None, None)
        snap = self.comp.db.pr("o/r#1")
        files = []
        with mock.patch.object(self.agent, "find_pr_session", return_value=None) as lookup, \
                mock.patch.object(self.agent, "session_files", side_effect=lambda path: files):
            self.comp.ensure_pin(snap)
            self.comp.ensure_pin(snap)
            self.assertEqual(lookup.call_count, 1)
            transcript = Path(self.checkout) / "s.jsonl"
            transcript.write_text("{}")
            files.append(("s", transcript))
            self.comp.ensure_pin(snap)
            self.assertEqual(lookup.call_count, 2)

    def test_recent_merges_are_searched_every_so_often_or_on_refresh(self):
        with mock.patch.object(self.comp.poller, "recent_merged", return_value=[{"number": 9}]) as search:
            self.poll()
            self.poll()
            self.assertEqual((search.call_count, self.comp.merged_version), (2, 1))  # one per list, once
            self.comp.check_now(merged=True)
            self.poll()
            self.assertEqual(search.call_count, 4)

    def test_a_views_older_merges_are_reused_by_other_tabs(self):
        page = {"prs": [], "after": None}
        with mock.patch.object(self.comp.poller, "merged", return_value=page) as search:
            self.comp.merged_history("o/r", "mine")
            self.comp.merged_history("o/r ", "mine")
            self.assertEqual(search.call_count, 1)


    def test_insights_search_recent_merges_once_per_view(self):
        node = pr_node(state="MERGED", merged_at="2026-10-20T00:00:00Z")
        self.gh.put(node, "author:@me")
        with mock.patch.object(self.gh, "graphql", wraps=self.gh.graphql) as gql:
            out = self.comp.insights("")
            self.comp.insights("")
        self.assertEqual(out["merged"], {"mine": [{"created": node["createdAt"], "merged": "2026-10-20T00:00:00Z"}],
                                         "review": []})
        self.assertEqual(gql.call_count, 2)  # one search per list, then the cache
        self.assertIn(f"merged:>={out['since']}", gql.call_args_list[0].args[1]["q"])


class Settings(OrchestratorCase):
    def test_views_save_to_the_config_file_and_keep_the_rest(self):
        self.config_path.write_text(json.dumps({"roots": ["~/dev"]}))
        self.comp.save_setting(["views"], ["o/r ,acme", "o/r, acme", "me"], expect=[])
        self.assertEqual(self.config_path.read_text(), '{\n  "roots": ["~/dev"],\n  "views": ["o/r, acme", "me"]\n}\n')
        self.assertEqual(self.comp.set_current_view("gone")["current"], "")  # everything
        self.assertEqual(self.comp.set_current_view("me")["current"], "me")
        for path, value, expect in ((["views"], ["acme is:open"], MISSING), (["auto", "post_reply"], True, MISSING),
                                    (["views"], ["x"], ["stale"])):
            with self.assertRaises(ValueError):
                self.comp.save_setting(path, value, expect=expect)

    def test_a_setting_saves_in_the_files_own_form_and_resets_away(self):
        self.config_path.write_text("{}")
        out = self.comp.save_setting(["auto", "rerun_checks", "when"], "medium", expect="high")
        self.assertEqual(json.loads(self.config_path.read_text()), {"auto": {"rerun_checks": {"when": "confidence >= medium"}}})
        self.assertEqual(read_config(self.config_path)["auto"]["rerun_checks"]["min_confidence"], "medium")
        field = next(f for f in out["fields"] if f["path"] == ["auto", "rerun_checks", "when"])
        self.assertEqual((field["value"], field["default"], field["set"]), ("medium", "high", True))
        self.comp.save_setting(["auto", "rerun_checks", "when"], reset=True)
        self.assertEqual(json.loads(self.config_path.read_text()), {})

    def test_switching_the_app_drops_the_old_apps_pinned_defaults(self):
        self.config_path.write_text(json.dumps({"launcher": {"name": "VS Code", "mode": "command", "url": "vscode://file/{path}"},
                                                "agent": {"name": "Claude Code", "url": "my://{session}"}}))
        self.comp.save_setting(["launcher", "app"], "cursor", expect="vscode")
        self.comp.save_setting(["agent", "sessions"], "codex", expect="claude-code")
        self.assertEqual(json.loads(self.config_path.read_text()),
                         {"launcher": {"mode": "command", "app": "cursor"}, "agent": {"url": "my://{session}", "sessions": "codex"}})
        self.assertEqual((self.comp.config["launcher"]["url"], self.comp.config["agent"]["name"]), ("cursor://file/{path}", "Codex"))

    def test_folders_from_settings_apply_without_a_restart(self):
        self.config_path.write_text("{}")
        comp = Orchestrator(validate_config({}), [], self.db_path, github=self.gh, config_path=self.config_path)
        self.assertEqual(comp.page_state()["roots"], [])
        with mock.patch("shipyard.orchestrator.worktrees.discover", return_value=[]) as scan:
            comp.save_setting(["roots"], self.checkout)
        self.assertEqual(comp.roots, [os.path.realpath(self.checkout)])
        scan.assert_called_once_with(comp.roots, None)

    def test_views_saved_in_the_database_move_to_the_config_file(self):
        self.comp.db.set_current_view("")
        self.comp.db._exec("INSERT INTO meta (key, value) VALUES ('views', ?)",
                           (json.dumps({"views": ["acme"], "current": "acme"}),))
        self.comp.move_old_views()
        self.assertEqual(json.loads(self.config_path.read_text())["views"], ["acme"])
        self.assertEqual(self.comp.views(), {"views": ["acme"], "current": "acme"})


class PageState(OrchestratorCase):
    def test_carries_pr_state_and_open_thread_count_for_the_page(self):
        self.gh.put(pr_node(threads=[{"body": "a"}, {"body": "b", "resolved": True}]), "author:@me")
        pr = self.poll()["o/r#1"]
        self.assertEqual((pr["state"], pr["dropped"], pr["openThreads"]), ("OPEN", False, 1))
        self.assertEqual((pr["pr"]["number"], pr["pr"]["headRefName"]), (1, "feature"))
        self.assertEqual(pr["lists"], ["mine"])
        self.assertEqual([w["branch"] for w in self.comp.page_state()["worktrees"]], ["feature"])


    def test_carries_when_your_review_was_requested(self):
        self.gh.put(pr_node(author="alice", requested=["me"], requested_at={"me": "2026-10-22T08:00:00Z"}),
                    "review-requested:@me")
        self.assertEqual(self.poll()["o/r#1"]["waitingSince"], "2026-10-22T08:00:00Z")


class AfterAction(OrchestratorCase):
    def test_rereads_the_pr_for_the_page_before_the_poll_does(self):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        self.gh.put(pr_node(state="MERGED"), "author:@me")
        self.comp.do_action("o/r#1", "merge", {})
        self.assertEqual(self.comp.page_state()["prs"]["o/r#1"]["pr"]["state"], "MERGED")

    def test_turns_auto_merge_off_for_the_page(self):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        self.comp.do_action("o/r#1", "disable_auto_merge", {})
        self.assertEqual(self.gh.calls[-1][2], {"id": "PR_o/r_1"})
        self.assertFalse(self.comp.db.pr("o/r#1")["pending"]["auto_merge"])


class Stacks(OrchestratorCase):
    def test_a_branch_behind_the_pr_it_is_stacked_on_gets_an_update_step(self):
        self.gh.put(pr_node(1, head="p1", branch="parent"), "author:@me")
        self.gh.put(pr_node(2, head="c1", branch="child", base="parent"), "author:@me")
        self.gh.compare = {"ahead_by": 2}
        steps = self.poll()["o/r#2"]["steps"]
        self.assertEqual([s["label"] for s in steps], ["Update from #1"])
        self.assertIn(("GET", "/repos/o/r/compare/c1...p1", None), self.gh.calls)
        self.gh.calls.clear()
        self.poll()
        self.assertFalse([c for c in self.gh.calls if "/compare/" in c[1]], "an unchanged pair isn't compared again")
        self.comp.do_action("o/r#2", "update_branch", {"method": "rebase"})
        self.assertEqual(self.gh.calls[-1][2]["m"], "REBASE")
        self.assertEqual(self.comp.page_state()["prs"]["o/r#2"]["steps"], [])


class ChangesRequested(OrchestratorCase):
    def setUp(self):
        super().setUp()
        self.raw = {"rules": {"passes": {"feedback": "auto"}}}
        self.comp.config = validate_config(self.raw)

    def test_pass_resumes_pinned_session_and_card_posts_the_edited_reply(self):
        self.agent.answers["reviewer requested changes"] = {
            "summary": "Rev wants a guard and asks why.",
            "proposals": [{"action": "continue", "confidence": "high", "reason": "needs code"},
                          {"action": "post_reply", "confidence": "high", "reason": "a reply answers it",
                           "payload": {"comment_id": 500, "body": "Because the cache is per-process."}},
                          {"action": "delete_repo", "confidence": "high", "reason": "nope"}]}
        self.gh.put(pr_node(decision="CHANGES_REQUESTED",
                            reviews=[{"state": "CHANGES_REQUESTED", "author": "rev"}],
                            threads=[{"body": "why per-process?", "path": "cache.py", "line": 7,
                                      "hunk": "@@ -1,9 +1,9 @@\n" + "\n".join(f" line {n}" for n in range(1, 9))}]),
                    "author:@me")
        prs = self.poll()
        pr = prs["o/r#1"]
        self.assertEqual(pr["cards"][0]["proposals"][1]["context"],
                         {"path": "cache.py", "line": 7, "outdated": False,
                          "hunk": "\n".join(f" line {n}" for n in range(3, 9))})  # the last 6 lines
        self.assertEqual(self.agent.runs[0]["mode"], "resume")
        self.assertEqual(self.agent.runs[0]["session"], "s-open")
        self.assertIn("comment_id=500", self.agent.runs[0]["prompt"])
        self.assertTrue(pr["planReady"])
        card = pr["cards"][0]
        self.assertEqual(card["kind"], "pass")
        self.assertEqual([p["action"] for p in card["proposals"]], ["continue", "post_reply"])
        self.assertIn("asked for a plan in the pinned session", [a["text"] for a in pr["activity"]])

        # the same state polled again runs nothing new
        self.poll()
        self.assertEqual(len(self.agent.runs), 1)

        self.comp.card_action(card["id"], 1, {"body": "Edited reply."})
        self.assertIn(("POST", "/repos/o/r/pulls/1/comments/500/replies", {"body": "Edited reply."}),
                      self.gh.calls)
        self.assertEqual(self.comp.page_state()["prs"]["o/r#1"]["cards"], [])
        # replied last on every open thread, before the poll sees the reply
        self.assertEqual([s["kind"] for s in self.comp.page_state()["prs"]["o/r#1"]["steps"]], ["rerequest_review"])

    def test_live_session_gets_a_kept_fork_instead_of_a_turn(self):
        self.agent.live = True
        self.gh.put(pr_node(decision="CHANGES_REQUESTED",
                            reviews=[{"state": "CHANGES_REQUESTED", "author": "rev"}]), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertEqual(self.agent.runs[0]["mode"], "fork")
        self.assertEqual(self.agent.discarded, [])
        self.assertEqual(card["kind"], "pass")
        self.assertEqual(card["kept"], {"path": self.checkout, "session": "fork-1", "fork": True})

    def test_live_session_without_fork_gets_a_kept_fresh_session(self):
        self.agent.live, self.agent.can_fork = True, False
        self.gh.put(pr_node(decision="CHANGES_REQUESTED",
                            reviews=[{"state": "CHANGES_REQUESTED", "author": "rev"}]), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertEqual(self.agent.runs[0]["mode"], "fresh")
        self.assertEqual(self.agent.discarded, [])
        self.assertEqual(card["kept"]["fork"], False)

    def test_card_goes_away_once_feedback_is_addressed(self):
        node = pr_node(decision="CHANGES_REQUESTED", reviews=[{"state": "CHANGES_REQUESTED", "author": "rev"}])
        self.gh.put(node, "author:@me")
        self.assertEqual(len(self.poll()["o/r#1"]["cards"]), 1)
        node["reviewDecision"] = "APPROVED"
        node["updatedAt"] = "2026-10-21T00:00:00Z"
        self.assertEqual(self.poll()["o/r#1"]["cards"], [])

    def test_approval_comments_outlast_an_auto_merge_until_answered(self):
        node = pr_node(requested=["rev"])
        self.gh.put(node, "author:@me")
        self.poll()
        node.update(state="MERGED", reviewDecision="APPROVED", updatedAt="2026-10-21T00:00:00Z",
                    mergedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    latestReviews={"nodes": [{"databaseId": 7, "state": "APPROVED", "body": "I'd prefer a Map here",
                                              "submittedAt": "2026-10-21T00:00:00Z", "author": {"login": "rev"},
                                              "commit": {"oid": "aaa"}}]})
        self.gh.put(node)
        self.assertEqual([c["pass"] for c in self.poll()["o/r#1"]["cards"]], ["feedback"])
        node["comments"] = {"nodes": [{"createdAt": "2026-10-21T01:00:00Z", "author": {"login": "me"}}]}
        self.assertEqual(self.poll()["o/r#1"]["cards"], [])


class CiFailed(OrchestratorCase):
    def setUp(self):
        super().setUp()
        self.agent.answers["CI failure"] = {
            "summary": "npm registry timed out; unrelated to the diff.",
            "proposals": [{"action": "rerun_checks", "confidence": "high", "reason": "looked flaky"}]}

    def red(self, head="aaa"):
        return pr_node(head=head, ci="FAILURE", contexts=[check_run("rspec", "FAILURE", job=11, run=22)])

    def test_fork_and_log_tail_in_prompt(self):
        self.trust("rerun_checks", False)
        self.gh.put(self.red(), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertEqual(self.agent.runs[0]["mode"], "fork")
        self.assertIn("ETIMEDOUT", self.agent.runs[0]["prompt"])
        self.assertEqual(self.agent.discarded, ["fork-1"])  # the fork's transcript is not kept
        self.assertEqual(card["proposals"][0]["action"], "rerun_checks")
        self.assertFalse(any(c[0] == "POST" for c in self.gh.calls))  # manual until trusted

    def test_rerun_from_the_card_closes_it(self):
        self.trust("rerun_checks", False)
        self.gh.put(self.red(), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.comp.do_action("o/r#1", "rerun_checks", {}, card["id"])
        self.assertEqual(self.comp.page_state()["prs"]["o/r#1"]["cards"], [])
        self.assertEqual(self.gh.calls[-1][1], "/repos/o/r/actions/runs/22/rerun-failed-jobs")

    def test_rerun_while_the_run_is_going_waits_for_it(self):
        self.trust("rerun_checks", False)
        node = pr_node(ci="FAILURE", contexts=[check_run("rspec", "FAILURE", job=11, run=22)], running=2)
        self.gh.put(node, "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.gh.running_runs.add(22)
        self.assertTrue(self.comp.do_action("o/r#1", "rerun_checks", {}, card["id"])["queued"])
        self.assertTrue(self.comp.page_state()["prs"]["o/r#1"]["cards"][0]["auto_wait"]["manual"])
        self.gh.running_runs.clear()
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["checkRunCountsByState"] = []
        self.poll()
        reruns = [c for c in self.gh.calls if c[1] == "/repos/o/r/actions/runs/22/rerun-failed-jobs"]
        self.assertEqual(len(reruns), 2)
        self.assertEqual(self.comp.page_state()["prs"]["o/r#1"]["cards"], [])

    def test_rerun_while_the_run_is_going_says_so_with_queueing_off(self):
        self.trust("rerun_checks", False)
        self.comp.config["rules"]["queue_rerun"] = False
        self.gh.put(self.red(), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.gh.running_runs.add(22)
        with self.assertRaisesRegex(RuntimeError, "still going"):
            self.comp.do_action("o/r#1", "rerun_checks", {}, card["id"])

    def test_trusted_rerun_fires_itself_and_lands_in_handled(self):
        self.gh.put(self.red(), "author:@me")
        pr = self.poll()["o/r#1"]
        self.assertIn(("POST", "/repos/o/r/actions/runs/22/rerun-failed-jobs", None), self.gh.calls)
        self.assertEqual(pr["cards"], [])
        self.assertEqual(pr["handled"][0]["handled"]["reason"], "looked flaky")
        self.assertFalse(pr["handled"][0]["handled"]["undoable"])

    def test_no_auto_action_when_the_card_closed_while_the_pass_ran(self):
        self.trust("rerun_checks")
        node = self.red()
        self.gh.put(node, "author:@me")

        def ci_recovers():  # CI goes green and the card closes before the agent answers
            for card in self.comp.db.cards("o/r#1", ("open",)):
                self.comp.db.update_card(card["id"], status="done")
        self.agent.before_answer = ci_recovers
        self.poll()
        self.assertFalse(any("rerun" in c[1] for c in self.gh.calls if c[0] == "POST"))
        self.assertEqual(self.comp.page_state()["prs"]["o/r#1"]["handled"], [])

    def test_no_auto_action_when_the_head_moved(self):
        self.trust("rerun_checks")
        self.gh.put(self.red(), "author:@me")

        def pushed():
            snap = self.comp.db.pr("o/r#1")
            self.comp.db.save_pr({**snap, "head": "newer"})
        self.agent.before_answer = pushed
        self.poll()
        self.assertFalse(any("rerun" in c[1] for c in self.gh.calls if c[0] == "POST"))

    def test_a_card_acts_once(self):
        self.trust("rerun_checks", False)
        self.gh.put(self.red(), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.comp.card_action(card["id"], 0, None)
        with self.assertRaises(LookupError):
            self.comp.card_action(card["id"], 0, None)
        self.assertEqual(len([c for c in self.gh.calls if "rerun" in c[1]]), 1)

    def test_max_per_pr_caps_automatic_reruns(self):
        self.trust("rerun_checks")
        for head in ("a1", "a2", "a3"):
            node = self.red(head)
            node["updatedAt"] = f"2026-10-2{head[-1]}T00:00:00Z"
            self.gh.put(node, "author:@me")
            self.poll()
        reruns = [c for c in self.gh.calls if c[0] == "POST" and "rerun" in c[1]]
        self.assertEqual(len(reruns), 2)

    def test_trusted_rerun_waits_for_running_checks(self):
        self.trust("rerun_checks")
        node = pr_node(ci="FAILURE", contexts=[check_run("rspec", "FAILURE", job=11, run=22)], running=2)
        self.gh.put(node, "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertFalse(any("rerun" in c[1] for c in self.gh.calls))
        self.assertEqual(card["auto_wait"]["checks"], ["rspec"])
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["checkRunCountsByState"] = []
        self.poll()
        self.assertIn(("POST", "/repos/o/r/actions/runs/22/rerun-failed-jobs", None), self.gh.calls)

    def test_waiting_rerun_holds_when_more_checks_fail(self):
        self.trust("rerun_checks")
        node = pr_node(ci="FAILURE", contexts=[check_run("rspec", "FAILURE", job=11, run=22)], running=2)
        self.gh.put(node, "author:@me")
        self.poll()
        self.gh.put(pr_node(ci="FAILURE", contexts=[check_run("rspec", "FAILURE", job=11, run=22),
                                                    check_run("lint", "FAILURE", job=12, run=22)]), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertFalse(any("rerun" in c[1] for c in self.gh.calls))
        self.assertEqual(card["auto_held"], "more checks failed while it waited")
        self.assertEqual(card["auto_action"], "rerun_checks")

    def test_fix_without_a_commit_message_gets_one_from_its_reason(self):
        self.agent.answers["CI failure"] = {"summary": "Typo.", "proposals": [
            {"action": "apply_fix", "confidence": "high", "reason": "Fixes the typo in app.py, so rspec loads.",
             "payload": {"diff": "x"}}]}
        self.gh.put(self.red(), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertEqual(card["proposals"][0]["payload"]["message"], "Fixes the typo in app.py")

    def test_setup_checks_say_what_to_fix(self):
        checks = lambda: [(c["id"], c["ok"], "fix" in c) for c in self.comp.setup_checks()["checks"]]
        self.assertEqual(checks(), [("github", True, False), ("agent", True, False)])
        with mock.patch.object(self.gh, "rest", side_effect=GitHubError("no token; run `gh auth login`")), \
                mock.patch.object(self.agent, "version", side_effect=AgentError("not found")):
            self.assertEqual(checks(), [("github", False, True), ("agent", False, True)])

    def test_a_new_install_offers_passes_and_presses_nothing_until_turned_on(self):
        self.comp.db.close()
        self.db_path.unlink()
        self.comp = Orchestrator(validate_config({}), [self.checkout], self.db_path, github=self.gh,
                                 config_path=self.config_path, first_run_hold=True)
        self.addCleanup(self.comp.db.close)
        self.gh.put(self.red(), "author:@me")
        state = self.poll()
        self.assertEqual(([c["kind"] for c in state["o/r#1"]["cards"]], self.agent.runs, self.comp.page_state()["automation"]),
                         (["offer"], [], "held"))
        restarted = Orchestrator(validate_config({}), [self.checkout], self.db_path, github=self.gh,
                                 config_path=self.config_path, first_run_hold=True)
        self.addCleanup(restarted.db.close)
        self.assertEqual((restarted.held(), restarted.page_state()["welcome"]), (True, True))  # polled since, still new
        self.comp.turn_on_automation()
        self.assertEqual(self.comp.page_state()["automation"], "on")
        again = Orchestrator(validate_config({}), [self.checkout], self.db_path, github=self.gh,
                             config_path=self.config_path, first_run_hold=True)
        self.addCleanup(again.db.close)
        self.assertFalse(again.held())

    def test_an_install_that_has_polled_before_isnt_held(self):
        self.gh.put(self.red(), "author:@me")
        self.poll()
        self.comp.db._exec("DELETE FROM meta WHERE key = 'automation'")
        again = Orchestrator(validate_config({}), [self.checkout], self.db_path, github=self.gh,
                             config_path=self.config_path, first_run_hold=True)
        self.addCleanup(again.db.close)
        self.assertFalse(again.held())

    def test_applied_fix_hides_fix_ci_before_the_next_poll(self):
        self.trust("rerun_checks", False)
        self.gh.put(self.red(), "author:@me")
        self.assertIn("fix_ci", [s["kind"] for s in self.poll()["o/r#1"]["steps"]])
        with mock.patch("shipyard.orchestrator.actions.run", return_value="applied the fix and pushed"):
            self.comp.do_action("o/r#1", "apply_fix", {"diff": "x"})
        self.assertNotIn("fix_ci", [s["kind"] for s in self.comp.page_state()["prs"]["o/r#1"]["steps"]])
        self.gh.put(self.red(head="bbb"), "author:@me")  # the fix failed CI too
        self.assertIn("fix_ci", [s["kind"] for s in self.poll()["o/r#1"]["steps"]])

    def test_trusted_fix_holds_when_it_edits_files_outside_the_pr(self):
        self.trust("apply_fix")
        diff = "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        self.agent.answers["CI failure"] = {"summary": "Typo.", "proposals": [
            {"action": "apply_fix", "confidence": "high", "reason": "fix typo", "payload": {"diff": diff}}]}
        self.gh.put(self.red(), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertEqual(card["auto_held"], "it edits other.py, which the PR doesn't change")

    def test_ask_mode_offers_instead_of_running(self):
        self.raw = {"rules": {"passes": {"ci_failed": "ask"}}, "auto": {"rerun_checks": False}}
        self.comp.config = validate_config(self.raw)
        self.gh.put(self.red(), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertEqual(card["kind"], "offer")
        self.assertEqual(self.agent.runs, [])
        self.comp.run_pass_now("o/r#1", "ci_failed")
        self.idle()
        self.assertEqual(self.comp.page_state()["prs"]["o/r#1"]["cards"][0]["kind"], "pass")


class Reviews(OrchestratorCase):
    def test_pre_review_runs_fresh_in_review_worktree_and_pins_it(self):
        self.agent.answers["first-pass code review"] = {
            "summary": "Solid; one edge case.",
            "proposals": [{"action": "submit_review", "confidence": "medium", "reason": "minor",
                           "payload": {"event": "COMMENT", "body": "Looks good; one question.",
                                       "comments": [{"path": "app.py", "line": 3, "body": "None here?"}]}}]}
        self.gh.put(pr_node(number=5, author="alice", requested=[VIEWER]), "review-requested:@me")
        pr = self.poll()["o/r#5"]
        run = self.agent.runs[0]
        self.assertEqual((run["mode"], run["cwd"]), ("fresh", self.checkout))
        self.assertTrue(run["untrusted"])  # their checkout's agent settings don't load
        self.assertEqual(self.comp.db.pin("o/r#5")["how"], "shipyard")
        self.assertEqual(self.agent.discarded, [])  # the pinned review session stays
        method, path, body = self.gh.calls[-1]
        self.assertEqual((method, path), ("POST", "/repos/o/r/pulls/5/reviews"))
        self.assertNotIn("event", body)  # pending: only you see it until you submit on GitHub
        self.assertEqual(body["comments"][0]["side"], "RIGHT")
        self.assertEqual(pr["cards"][0]["proposals"][0]["action"], "finish_on_github")

    def test_clean_approval_stays_a_button_at_any_confidence(self):
        self.agent.answers["first-pass code review"] = {"summary": "Fine.", "proposals": [
            {"action": "submit_review", "confidence": "medium", "reason": "tiny and tested",
             "payload": {"event": "APPROVE", "body": "LGTM"}}]}
        self.gh.put(pr_node(number=5, author="alice", requested=[VIEWER]), "review-requested:@me")
        pr = self.poll()["o/r#5"]
        self.assertFalse(any(c[1].endswith("/reviews") for c in self.gh.calls))
        self.assertEqual(pr["cards"][0]["proposals"][0]["action"], "approve")


    def test_approving_from_a_card_records_the_review_before_the_next_poll(self):
        self.agent.answers["first-pass code review"] = {"summary": "Fine.", "proposals": [
            {"action": "submit_review", "confidence": "medium", "reason": "tiny",
             "payload": {"event": "APPROVE", "body": "LGTM"}}]}
        self.gh.put(pr_node(number=5, author="alice", requested=[VIEWER]), "review-requested:@me")
        card = self.poll()["o/r#5"]["cards"][0]
        self.comp.card_action(card["id"], 0, None)
        pr = self.comp.page_state()["prs"]["o/r#5"]
        self.assertNotIn("review", [s["kind"] for s in pr["steps"]])
        self.assertEqual(self.comp.db.pr("o/r#5")["my_review"]["state"], "APPROVED")

    def test_re_review_without_a_review_worktree_makes_one(self):
        from shipyard.orchestrator import worktrees
        review = {"databaseId": 9, "state": "CHANGES_REQUESTED", "submittedAt": "2026-10-19T00:00:00Z",
                  "commit": {"oid": "old"}}
        self.gh.put(pr_node(number=6, author="bob", requested=[VIEWER], my_review=review), "review-requested:@me")
        pr = self.poll()["o/r#6"]
        worktrees.review_worktree.assert_called()
        self.assertEqual(self.agent.runs[0]["cwd"], self.checkout)
        self.assertEqual(pr["cards"][0]["kind"], "pass")

    def test_re_review_drafts_unresolved_comment_as_pending_review(self):
        self.agent.answers = {"re-review": {"summary": "one left", "proposals": [
            {"action": "comment_unresolved", "confidence": "medium", "reason": "README", "payload": {"body": "Fix README"}},
            {"action": "approve", "confidence": "low", "reason": "minor", "payload": {"body": "ok"}}]}}
        review = {"databaseId": 9, "state": "CHANGES_REQUESTED", "submittedAt": "2026-10-19T00:00:00Z",
                  "commit": {"oid": "old"}}
        self.gh.put(pr_node(number=6, author="bob", requested=[VIEWER], my_review=review), "review-requested:@me")
        pr = self.poll()["o/r#6"]
        method, path, body = self.gh.calls[-1]
        self.assertEqual((path, body.get("body")), ("/repos/o/r/pulls/6/reviews", "Fix README"))
        self.assertNotIn("event", body)
        self.assertEqual([p["action"] for p in pr["cards"][0]["proposals"]], ["finish_on_github"])


    def test_trusted_approve_only_from_re_review_and_without_agent_text(self):
        self.trust("approve_rereview", {"min_wait_minutes": 0})
        self.agent.answers["first-pass code review"] = {"summary": "ok", "proposals": [
            {"action": "approve", "confidence": "high", "reason": "fine", "payload": {"body": "LGTM!!"}}]}
        self.gh.put(pr_node(number=5, author="alice", requested=[VIEWER]), "review-requested:@me")
        self.poll()
        self.assertFalse(any(c[1].endswith("/reviews") for c in self.gh.calls))  # a first review stays manual
        self.agent.answers = {"re-review": {"summary": "all addressed", "proposals": [
            {"action": "approve", "confidence": "high", "reason": "threads fixed", "payload": {"body": "agent words"}}]}}
        review = {"databaseId": 9, "state": "APPROVED", "submittedAt": "2026-10-19T00:00:00Z",
                  "commit": {"oid": "old"}}
        self.gh.put(pr_node(number=7, author="bob", requested=[VIEWER], my_review=review), "review-requested:@me")
        self.poll()
        posted = [c for c in self.gh.calls if c[1] == "/repos/o/r/pulls/7/reviews"]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][2]["event"], "APPROVE")
        self.assertNotIn("body", posted[0][2])
        self.assertIn("approved automatically: threads fixed",
                      [a["text"] for a in self.comp.page_state()["prs"]["o/r#7"]["activity"]])

    def rereview_approval_posted(self, files, state="APPROVED", dismissed_from=None, auto_approve=None,
                                 wait=0):
        self.trust("approve_rereview", {"min_wait_minutes": wait})
        if auto_approve is not None:
            self.comp.db.set_auto_approve("o/r#7", auto_approve, "you")
        self.gh.compare = {"files": files}
        self.agent.answers = {"re-review": {"summary": "all addressed", "proposals": [
            {"action": "approve", "confidence": "high", "reason": "threads fixed"}]}}
        review = {"databaseId": 9, "state": state, "submittedAt": "2026-10-19T00:00:00Z",
                  "commit": {"oid": "old"}}
        node = pr_node(number=7, author="bob", requested=[VIEWER], my_review=review)
        if state == "DISMISSED":
            node["timelineItems"]["nodes"].append({"__typename": "ReviewDismissedEvent",
                                                   "previousReviewState": dismissed_from, "review": {"databaseId": 9}})
        self.gh.put(node, "review-requested:@me")
        self.poll()
        return any(c[1] == "/repos/o/r/pulls/7/reviews" for c in self.gh.calls)

    def test_trusted_approve_fires_on_a_small_change(self):
        self.assertTrue(self.rereview_approval_posted(
            [{"filename": "a.py", "status": "modified", "additions": 20, "deletions": 10}]))

    def test_trusted_approve_waits_a_few_minutes_after_the_push(self):
        files = [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 1}]
        self.assertFalse(self.rereview_approval_posted(files, wait=3))
        card = self.comp.db.cards("o/r#7", ("open",))[-1]
        self.assertIn("until", card["auto_wait"])
        self.comp.db._exec("UPDATE events SET at = '2000-01-01T00:00:00Z' WHERE key = 'o/r#7'")
        self.comp.db.update_card(card["id"], auto_wait={**card["auto_wait"], "until": "2000-01-01T00:03:00Z"})
        self.poll()
        self.assertTrue(any(c[1] == "/repos/o/r/pulls/7/reviews" for c in self.gh.calls))

    def test_trusted_approve_holds_back_unless_you_approved_before(self):
        self.assertFalse(self.rereview_approval_posted(
            [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 1}], state="CHANGES_REQUESTED"))

    def test_trusted_approve_fires_after_changes_requested_when_turned_on(self):
        self.assertTrue(self.rereview_approval_posted(
            [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 1}],
            state="CHANGES_REQUESTED", auto_approve=True))

    def test_trusted_approve_holds_back_when_turned_off(self):
        self.assertFalse(self.rereview_approval_posted(
            [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 1}], auto_approve=False))

    def test_your_auto_approve_choice_beats_the_pre_review(self):
        self.agent.answers["first-pass code review"] = {"summary": "Fine.", "proposals": [],
                                                        "auto_approve": {"on": True, "why": "one rename"}}
        self.gh.put(pr_node(number=5, author="alice", requested=[VIEWER]), "review-requested:@me")
        self.poll()
        self.assertEqual(self.comp.db.auto_approve("o/r#5")["why"], "one rename")
        self.comp.set_auto_approve("o/r#5", False)
        self.comp.run_pass_now("o/r#5", "pre_review")
        self.idle()
        self.assertEqual(len(self.agent.runs), 2)
        self.assertEqual({k: v for k, v in self.comp.db.auto_approve("o/r#5").items() if k in ("on", "by")},
                         {"on": False, "by": "you"})

    def test_auto_approve_stays_hidden_until_something_decides_it(self):
        self.agent.answers["first-pass code review"] = {"summary": "Fine.", "proposals": []}
        self.gh.put(pr_node(number=5, author="alice", requested=[VIEWER]), "review-requested:@me")
        self.poll()
        self.assertIsNone(self.comp.page_state()["prs"]["o/r#5"]["autoApprove"])
        self.comp.set_auto_approve("o/r#5", True)
        self.assertEqual(self.comp.page_state()["prs"]["o/r#5"]["autoApprove"]["by"], "you")

    def test_held_auto_approval_says_why_on_the_card(self):
        self.assertFalse(self.rereview_approval_posted(
            [{"filename": "a.py", "status": "modified", "additions": 25, "deletions": 6}]))
        card = self.comp.page_state()["prs"]["o/r#7"]["cards"][0]
        self.assertEqual(card["auto_held"], "31 lines changed since your review (limit 30)")

    def test_a_rereview_that_does_not_approve_says_so_with_auto_approve_on(self):
        self.trust("approve_rereview")
        self.agent.answers = {"re-review": {"summary": "one thread left", "proposals": []}}
        review = {"databaseId": 9, "state": "APPROVED", "submittedAt": "2026-10-19T00:00:00Z", "commit": {"oid": "old"}}
        self.gh.put(pr_node(number=7, author="bob", requested=[VIEWER], my_review=review), "review-requested:@me")
        self.poll()
        card = self.comp.page_state()["prs"]["o/r#7"]["cards"][0]
        self.assertEqual(card["auto_held"], "the re-review didn't recommend approving")

    def test_trusted_approve_fires_after_your_approval_was_dismissed(self):
        small = [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 1}]
        self.assertTrue(self.rereview_approval_posted(small, state="DISMISSED", dismissed_from="APPROVED"))

    def test_trusted_approve_holds_back_on_a_large_change(self):
        self.assertFalse(self.rereview_approval_posted(
            [{"filename": "a.py", "status": "modified", "additions": 25, "deletions": 6}]))

    def test_trusted_approve_holds_back_when_a_file_is_added(self):
        self.assertFalse(self.rereview_approval_posted(
            [{"filename": "new.py", "status": "added", "additions": 2, "deletions": 0}]))

    def test_trusted_actions_never_fire_on_someone_elses_pr(self):
        self.trust("rerun_checks")
        self.agent.answers["re-review"] = {"summary": "flaky", "proposals": [
            {"action": "rerun_checks", "confidence": "high", "reason": "flaky"}]}
        review = {"databaseId": 9, "state": "COMMENTED", "submittedAt": "2026-10-19T00:00:00Z", "commit": {"oid": "old"}}
        self.gh.put(pr_node(number=8, author="bob", requested=[VIEWER], my_review=review, ci="FAILURE",
                            contexts=[check_run("rspec", "FAILURE")]), "review-requested:@me")
        self.poll()
        self.assertFalse(any("rerun" in c[1] for c in self.gh.calls))


class Proposals(OrchestratorCase):
    def test_push_after_changes_requested_proposes_re_request(self):
        node = pr_node(decision="CHANGES_REQUESTED", reviews=[{"state": "CHANGES_REQUESTED", "author": "jm"}])
        self.gh.put(node, "author:@me")
        self.poll()
        node["headRefOid"] = "bbb"
        node["commits"]["nodes"][0]["commit"]["oid"] = "bbb"
        self.gh.put(node, "author:@me")
        cards = self.poll()["o/r#1"]["cards"]
        proposal = next(c for c in cards if c["kind"] == "proposal")
        self.assertEqual(proposal["title"], "Re-request jm's review")
        self.comp.card_action(proposal["id"], 0, None)
        self.assertIn(("POST", "/repos/o/r/pulls/1/requested_reviewers",
                       {"reviewers": ["jm"], "team_reviewers": []}), self.gh.calls)
        # the step goes at once, before a poll sees the request
        self.assertEqual(self.comp.page_state()["prs"]["o/r#1"]["steps"], [])

class Dropped(OrchestratorCase):
    def test_pr_that_leaves_every_list_is_read_once_then_left(self):
        node = pr_node(number=5, author="alice", requested=[VIEWER])
        self.gh.put(node, "review-requested:@me")
        self.assertEqual(len(self.poll()["o/r#5"]["cards"]), 1)
        node["reviewRequests"]["nodes"] = []
        self.gh.put(node)  # request withdrawn: in no list, still open
        self.assertEqual(self.poll()["o/r#5"]["cards"], [])
        self.assertNotIn(node["id"], self.comp.db.open_node_ids())


class PageRequests(OrchestratorCase):
    def test_snooze_chip_and_repin(self):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        self.comp.snooze("o/r#1", 24)
        pr = self.comp.page_state()["prs"]["o/r#1"]
        self.assertEqual(pr["chip"]["kind"], "snoozed")
        self.comp.snooze("o/r#1", None)
        self.assertIsNone(self.comp.page_state()["prs"]["o/r#1"]["chip"])
        self.comp.repin("o/r#1", "s-other")
        self.assertEqual(self.comp.db.pin("o/r#1")["session_id"], "s-other")
        self.comp.unpin("o/r#1")
        self.poll()
        self.assertIsNone(self.comp.db.pin("o/r#1")["session_id"])  # the poll doesn't pin its guess again
        with self.assertRaises(LookupError):
            self.comp.repin("o/r#1", "not-in-this-checkout")

    def test_pin_found_once_the_worktree_appears_after_the_first_poll(self):
        self.gh.put(pr_node(), "author:@me")
        with mock.patch("shipyard.orchestrator.worktrees.discover", return_value=[]):
            self.poll()
        self.assertIsNone(self.comp.db.pin("o/r#1"))
        self.poll()
        self.assertEqual(self.comp.db.pin("o/r#1")["session_id"], "s-open")

    def test_ask_runs_a_fork_with_the_question(self):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        self.comp.ask("o/r#1", "  what did we decide about caching?  ")
        self.idle()
        self.assertEqual(self.agent.runs[-1]["mode"], "fork")
        self.assertIn("what did we decide about caching?", self.agent.runs[-1]["prompt"])
        pr = self.comp.page_state()["prs"]["o/r#1"]
        self.assertEqual(pr["cards"][0]["kind"], "answer")
        self.assertFalse(pr["planReady"])  # an answer to your question doesn't flag the board
        self.assertIn("asked “what did we decide about caching?”", [a["text"] for a in pr["activity"]])
        with self.assertRaises(ValueError):
            self.comp.ask("o/r#1", "   ")

    def ask_for_comment(self, question):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        self.agent.answers["Question:"] = {"summary": "Rev approved it.", "proposals": [
            {"action": "post_reply", "confidence": "high", "reason": "asked", "payload": {"body": "Thanks for the review!"}}]}
        self.comp.ask("o/r#1", question)
        self.idle()
        posted = [c for c in self.gh.calls if c[0] == "POST" and c[1].endswith("/issues/1/comments")]
        return posted, self.comp.page_state()["prs"]["o/r#1"]

    def test_ask_posts_a_comment_it_quotes(self):
        posted, pr = self.ask_for_comment('Add "Thanks for the review!" on the PR')
        self.assertEqual(posted[0][2], {"body": "Thanks for the review!"})
        self.assertEqual(pr["cards"], [])
        self.assertEqual(pr["handled"][0]["handled"]["result"], "posted a comment")

    def test_ask_without_quoted_words_only_proposes(self):
        posted, pr = self.ask_for_comment("thank them for the review")
        self.assertEqual(posted, [])
        self.assertEqual(pr["cards"][0]["proposals"][0]["action"], "post_reply")

    def test_new_session_starts_fresh_and_replaces_the_pin(self):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        self.assertEqual(self.comp.db.pin("o/r#1")["session_id"], "s-open")
        self.comp.run_pass_now("o/r#1", "new_session")
        self.idle()
        self.assertEqual((self.agent.runs[-1]["mode"], self.agent.runs[-1]["name"]), ("fresh", "PR 1 (#1)"))
        self.assertEqual(self.comp.db.pin("o/r#1")["session_id"], "fresh-1")
        self.assertEqual(self.agent.discarded, [])
        self.assertEqual(self.comp.db.cards("o/r#1", ("open",)), [])  # no card; the pin is the result
        self.assertFalse(self.comp.page_state()["prs"]["o/r#1"]["planReady"])

    def test_new_session_reads_a_merged_pr_it_never_saw(self):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        self.gh.put(pr_node(number=9, state="MERGED"))
        self.comp.run_pass_now("o/r#9", "new_session", "PR_o/r_9")
        self.idle()
        self.assertEqual(self.comp.db.pin("o/r#9")["session_id"], "fresh-1")

    def test_unknown_pr(self):
        with self.assertRaises(LookupError):
            self.comp.do_action("o/r#404", "merge", {})


if __name__ == "__main__":
    unittest.main()


class ReloadConfig(OrchestratorCase):
    def test_a_hand_edit_is_picked_up_and_a_bad_one_is_ignored(self):
        edited = validate_config({"rules": {"merge": False}})
        with mock.patch("shipyard.orchestrator.config_mtime", return_value=1.0), \
                mock.patch("shipyard.orchestrator.read_config", return_value=edited):
            self.comp.reload_config()
        self.assertFalse(self.comp.config["rules"]["merge"])
        with mock.patch("shipyard.orchestrator.config_mtime", return_value=2.0), \
                mock.patch("shipyard.orchestrator.read_config", side_effect=ValueError("bad")):
            self.comp.reload_config()
        self.assertIs(self.comp.config, edited)


class Prune(OrchestratorCase):
    def test_a_forgotten_pr_takes_its_rows_with_it(self):
        db = self.comp.db
        self.gh.put(pr_node(ci="FAILURE", contexts=[check_run("rspec", "FAILURE", job=1, run=2)]), "author:@me")
        self.poll()
        db.log("o/r#1", "x")
        db._exec("UPDATE prs SET seen = 0, snapshot = json_set(snapshot, '$.state', 'MERGED')")
        db.prune()
        for table in ("prs", "events", "cards", "passes", "activity"):
            self.assertEqual(db._rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"], 0, table)

    def test_pre_review_refuses_your_own_pr(self):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        card = self.comp.run_pass_now("o/r#1", "pre_review")["card"]
        self.idle()
        self.assertIn("this one is yours", self.comp.db.card(card)["error"])
