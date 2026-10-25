"""The companion's memory, in one SQLite file next to shipyard.config.json: the last
snapshot of each PR, events already handled, pins, snoozes, cards, passes, activity,
and how often each action ran automatically on each PR."""
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (key TEXT PRIMARY KEY, node_id TEXT, snapshot TEXT NOT NULL, seen REAL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, name TEXT NOT NULL, head TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL, data TEXT, UNIQUE (key, name, head));
CREATE TABLE IF NOT EXISTS pins (key TEXT PRIMARY KEY, path TEXT, session_id TEXT, how TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS snoozes (key TEXT PRIMARY KEY, until TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
  created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS passes (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, name TEXT NOT NULL, head TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL, mode TEXT, session_id TEXT, card_id INTEGER, question TEXT, error TEXT,
  summary TEXT, created TEXT NOT NULL, finished TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS passes_once ON passes (key, name, head) WHERE name != 'ask';
CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY, key TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auto_runs (key TEXT NOT NULL, action TEXT NOT NULL, count INTEGER NOT NULL,
  PRIMARY KEY (key, action));
CREATE INDEX IF NOT EXISTS cards_key ON cards (key, status);
CREATE INDEX IF NOT EXISTS passes_key ON passes (key);
CREATE INDEX IF NOT EXISTS activity_key ON activity (key, id);
"""
# Rows that belong to one PR, dropped with it.
PER_PR_TABLES = ("events", "cards", "passes", "activity", "pins", "snoozes", "auto_runs")
KEEP_DAYS = 14
ACTIVITY_PER_PR = 100


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            # A restart (under launchd, saving the companion's code reloads it) cuts running
            # passes short. Re-running one could add a second turn to your session, so
            # mark it failed; its card offers Try again.
            self.db.execute("UPDATE passes SET status = 'failed', error = 'interrupted by a restart' "
                            "WHERE status IN ('running', 'queued')")
            for row in self.db.execute("SELECT card_id FROM passes WHERE error = 'interrupted by a restart' "
                                       "AND card_id IS NOT NULL").fetchall():
                self.db.execute("UPDATE cards SET data = json_set(data, '$.kind', 'error', '$.error', "
                                "'Interrupted by a restart.') WHERE id = ? AND status = 'open' "
                                "AND json_extract(data, '$.kind') = 'running'", (row[0],))
            self.db.execute("UPDATE cards SET status = 'open' WHERE status = 'acting'")
            self.db.execute("DROP TABLE IF EXISTS clicks")
            # The feedback pass and its events were once named "changes_requested".
            for old, new in (("changes_requested", "feedback"), ("pushed_after_changes_requested", "pushed_after_feedback")):
                for table in ("events", "passes"):
                    self.db.execute(f"UPDATE OR IGNORE {table} SET name = ? WHERE name = ?", (new, old))
                    self.db.execute(f"DELETE FROM {table} WHERE name = ?", (old,))
            self.db.execute("UPDATE cards SET data = json_set(data, '$.pass', 'feedback') "
                            "WHERE json_extract(data, '$.pass') = 'changes_requested'")
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ---- PR snapshots
    def pr(self, key):
        rows = self._rows("SELECT snapshot FROM prs WHERE key = ?", (key,))
        return json.loads(rows[0]["snapshot"]) if rows else None

    def prs(self):
        return [json.loads(r["snapshot"]) for r in self._rows("SELECT snapshot FROM prs")]

    def save_pr(self, snap):
        self._exec("INSERT INTO prs (key, node_id, snapshot, seen) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET node_id = excluded.node_id, "
                   "snapshot = excluded.snapshot, seen = excluded.seen",
                   (snap["key"], snap["id"], json.dumps(snap), time.time()))

    def open_node_ids(self):
        """Open PRs still on one of your lists; a PR that dropped off every list (say a
        review request was withdrawn) is read once more, then left alone. A recently
        merged PR whose approval comments still hold a card keeps being read until you
        answer."""
        out = set()
        since = datetime.fromtimestamp(time.time() - KEEP_DAYS * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for r in self._rows("SELECT key, snapshot FROM prs"):
            snap = json.loads(r["snapshot"])
            if snap.get("state") == "OPEN" and not snap.get("dropped"):
                out.add(snap["id"])
            elif (snap.get("state") == "MERGED" and (snap.get("merged_at") or "") > since and
                  any(c.get("pass") == "feedback" for c in self.cards(r["key"], ("open",)))):
                out.add(snap["id"])
        return out

    def prune(self, days=KEEP_DAYS):
        """Closed or dropped PRs stay a while for Recently Merged cleanup, then go with
        everything kept about them. Finished cards and old activity go sooner."""
        cutoff = time.time() - days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            self.db.execute("DELETE FROM prs WHERE seen < ? AND (json_extract(snapshot, '$.state') != 'OPEN' "
                            "OR json_extract(snapshot, '$.dropped'))", (cutoff,))
            for table in PER_PR_TABLES:
                self.db.execute(f"DELETE FROM {table} WHERE key NOT IN (SELECT key FROM prs)")
            self.db.execute("DELETE FROM cards WHERE status IN ('done', 'dismissed') AND updated < ?", (cutoff_iso,))
            self.db.execute("DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity AS a WHERE "
                            "a.key = activity.key ORDER BY id DESC LIMIT ?)", (ACTIVITY_PER_PR,))
            self.db.commit()

    # ---- events
    def record_event(self, key, event):
        """True when the event is new for this PR and head; False when already handled."""
        try:
            self._exec("INSERT INTO events (key, name, head, at, data) VALUES (?, ?, ?, ?, ?)",
                       (key, event["name"], event.get("head") or "", now_iso(),
                        json.dumps(event.get("data") or {})))
            return True
        except sqlite3.IntegrityError:
            return False

    # ---- pins and snoozes
    def pin(self, key):
        rows = self._rows("SELECT * FROM pins WHERE key = ?", (key,))
        return rows[0] if rows else None

    def set_pin(self, key, path, session_id, how):
        self._exec("INSERT OR REPLACE INTO pins (key, path, session_id, how, at) VALUES (?, ?, ?, ?, ?)",
                   (key, path, session_id, how, now_iso()))

    def snooze(self, key):
        rows = self._rows("SELECT until FROM snoozes WHERE key = ?", (key,))
        return rows[0]["until"] if rows else None

    def set_snooze(self, key, until):
        if until:
            self._exec("INSERT OR REPLACE INTO snoozes (key, until) VALUES (?, ?)", (key, until))
        else:
            self._exec("DELETE FROM snoozes WHERE key = ?", (key,))

    # ---- cards
    def add_card(self, key, data, status="open"):
        t = now_iso()
        data = {**data, "created": data.get("created") or t}
        cur = self._exec("INSERT INTO cards (key, status, data, created, updated) VALUES (?, ?, ?, ?, ?)",
                         (key, status, json.dumps(data), t, t))
        return cur.lastrowid

    def delete_card(self, card_id):
        self._exec("DELETE FROM cards WHERE id = ?", (card_id,))

    def claim_card(self, card_id):
        """Atomically move an open card to 'acting' so one press acts once."""
        cur = self._exec("UPDATE cards SET status = 'acting', updated = ? WHERE id = ? AND status = 'open'",
                         (now_iso(), card_id))
        return cur.rowcount == 1

    def card(self, card_id):
        rows = self._rows("SELECT * FROM cards WHERE id = ?", (card_id,))
        return self._card(rows[0]) if rows else None

    @staticmethod
    def _card(row):
        return {**json.loads(row["data"]), "id": row["id"], "key": row["key"], "status": row["status"],
                "updated": row["updated"]}

    def update_card(self, card_id, status=None, **fields):
        with self.lock:
            card = self.card(card_id)
            if not card:
                return None
            data = {k: v for k, v in card.items() if k not in ("id", "key", "status", "updated")}
            data.update(fields)
            self._exec("UPDATE cards SET status = ?, data = ?, updated = ? WHERE id = ?",
                       (status or card["status"], json.dumps(data), now_iso(), card_id))
            return self.card(card_id)

    def cards(self, key=None, statuses=("open", "handled")):
        marks = ",".join("?" * len(statuses))
        sql = f"SELECT * FROM cards WHERE status IN ({marks})"
        args = list(statuses)
        if key:
            sql += " AND key = ?"
            args.append(key)
        return [self._card(r) for r in self._rows(sql + " ORDER BY id", args)]

    # ---- passes
    def queue_pass(self, key, name, head, mode, card_id, question=None):
        """The new pass id, or None when this pass already ran for this head. The card
        is attached in the same insert, so a worker never sees a pass without one."""
        try:
            cur = self._exec("INSERT INTO passes (key, name, head, status, mode, card_id, question, created) "
                             "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
                             (key, name, head or "", mode, card_id, question, now_iso()))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def claim_pass(self, pass_id):
        """Atomically move a queued pass to running; False when another thread got it."""
        cur = self._exec("UPDATE passes SET status = 'running' WHERE id = ? AND status = 'queued'", (pass_id,))
        return cur.rowcount == 1

    def pass_(self, pass_id):
        rows = self._rows("SELECT * FROM passes WHERE id = ?", (pass_id,))
        return rows[0] if rows else None

    def queued_passes(self):
        return self._rows("SELECT * FROM passes WHERE status = 'queued' ORDER BY id")

    def update_pass(self, pass_id, **fields):
        if fields.get("status") in ("done", "failed", "skipped"):
            fields.setdefault("finished", now_iso())
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE passes SET {sets} WHERE id = ?", (*fields.values(), pass_id))

    def passes(self, key):
        return self._rows("SELECT * FROM passes WHERE key = ? ORDER BY id", (key,))

    def shipyard_turns(self, session_id):
        """Turns Shipyard itself added to a session (forks add none)."""
        rows = self._rows("SELECT COUNT(*) AS n FROM passes WHERE session_id = ? AND status = 'done' "
                          "AND mode IN ('resume', 'fresh')", (session_id,))
        return rows[0]["n"]

    # ---- activity and trust
    def log(self, key, text):
        self._exec("INSERT INTO activity (key, at, text) VALUES (?, ?, ?)", (key, now_iso(), text))

    def last_logged(self, key, text):
        rows = self._rows("SELECT at FROM activity WHERE key = ? AND text = ? ORDER BY id DESC LIMIT 1", (key, text))
        return rows[0]["at"] if rows else None

    def activity(self, key, limit=30):
        return list(reversed(self._rows("SELECT at, text FROM activity WHERE key = ? ORDER BY id DESC LIMIT ?",
                                        (key, limit))))

    def auto_runs(self, key, action):
        rows = self._rows("SELECT count FROM auto_runs WHERE key = ? AND action = ?", (key, action))
        return rows[0]["count"] if rows else 0

    def count_auto_run(self, key, action):
        self._exec("INSERT INTO auto_runs (key, action, count) VALUES (?, ?, 1) "
                   "ON CONFLICT(key, action) DO UPDATE SET count = count + 1", (key, action))
