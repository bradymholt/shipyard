# Shipyard

A focused dashboard for staying on top of GitHub pull requests — reviewer state, CI
status, stacked PRs, and the work needing your attention, in one place. Runs locally,
served by its companion process. The simple hosted dashboard it grew out of lives on
separately as Harbor (https://github.com/bradymholt/harbor); Shipyard has no hosted mode.

Claude Code, Codex, and other agents all read this file. Never create a separate
`CLAUDE.md`.

## Architecture

- **The page** — vanilla HTML, CSS, and JS: no framework, no bundler, no build step, no
  dependencies to install. Edit the files directly. `index.html` is the markup, `app.css`
  the styles, and `js/` native ES modules, loaded from `js/main.js`: `base.js` (shared
  `state` and DOM handles; it imports nothing, so code that runs at load can rely on it),
  `server.js` (requests, the event stream, views, checkouts), `data.js` (PR lists and
  merged history), `prs.js` (review state, next steps, lanes), `board.js`, `sidebar.js`,
  `settings.js`, and `html.js` (icons, escaping, formatting). A module can't assign to
  another's export, so shared mutable values live on `state`.
- **`shipyard.py`** — thin launcher for the local companion (Python 3 stdlib
  only). Keep it as the entry point: the run command, the launchd plist, and these docs
  all point at it.
- **`shipyard/`** — the companion. It serves the page, pushes your local checkouts with
  its state, runs branch and worktree actions, and watches your PRs as a shipping
  orchestrator. The flow runs one
  way: `poll.py` (GitHub GraphQL, token from `gh auth token`) produces snapshots,
  `events.py` diffs them into named events, `rules.py` turns events into chips, passes,
  and proposed cards, and `companion.py` runs passes through `agents/` (one adapter per
  coding agent: Claude Code, Codex), stores everything in SQLite (`state.py`), and pushes
  the page's state over Server-Sent Events (`server.py`). `actions.py` holds the GitHub
  calls behind card buttons; `passes/*.md` are the prompt templates. Rules never call an
  agent directly, and nothing posts under the user's name without the page showing the
  text first. Binds to localhost; discovery is read-only.
- **`shipyard.config.json`** — local companion config (scan roots, app launcher, coding agent,
  `rules`, and the `auto` trust flags). Copy from `shipyard.config.example.json`; every
  setting is validated in `shipyard/config.py`.
- **`tests/`** — `python3 -m unittest discover tests` for the companion; `node
  tests/ui/smoke.mjs` drives the page in Chromium against `tests/ui/demo_server.py` (the
  real server over fake GitHub and agent data).
- **`shipyard-launchd.py`** — macOS-only setup helper that runs the companion as a
  `launchd` agent (`install`, `status`, `restart`, `logs`, `uninstall`). Manages two
  labels: `local.shipyard` for the companion and `local.shipyard-restart`, a `WatchPaths`
  agent that reloads it on save. A plist that is a symlink belongs to an external manager,
  so it is loaded but never rewritten. Nothing else depends on this file; the app never
  calls it. Keep platform-specific service code here rather than in `shipyard.py`.

The companion makes every GitHub call, with the `gh auth token` token; the page never
talks to GitHub or holds a token. The page builds its lists from the PRs the companion
pushes, filtered to the current view, including your latest merges, which the companion
searches every 15 minutes and pushes as their own event. It asks `/companion/merged` only
for a view's older merges (Show more), reads a PR's timeline from `/companion/timeline`,
and acts through `/companion/pr-action`.
Your saved views live in `shipyard.db` with the rest of the companion's state; only display
preferences (tab, sort, collapsed lanes) stay in the browser's local storage.

## Running it

```bash
python3 shipyard.py ~/dev     # serve + match PRs to clones under ~/dev
open http://localhost:4321
```

There is nothing to build or install.

## Design system

Colors, fonts, and spacing tokens are CSS custom properties in the `:root` block at the
top of `app.css` (`--green`, `--fg-muted`, `--purple`, `--sans`, etc.). **Use these
variables — never hardcode a hex color or font stack.** When adding UI, match the weight
and size of nearby elements so new pieces don't outshout existing ones (e.g. row meta is
`11px/500-600`, the PR title is `15px/600`).

## Workflow

Ship each change end to end without being asked: commit to a feature branch, open a PR, and
merge it with `gh pr merge --squash`. An unmerged PR is an unshipped change, and merging is the
standing instruction, not something to ask about. There is no CI, so check the change yourself
before merging: run the unit tests, and the browser smoke test for page changes. Run the smoke test once, after the change is
done; while a new check is failing, debug it with a short script that runs just that check, not
the full suite. Keep
unrelated work out of the commit, and stop rather than merge when something looks wrong.

The local companion serves the page from the main checkout, so a merge reaches it only
after that checkout pulls. Finish with `git pull --ff-only` there. The reload-on-save watcher
restarts the companion when the pull changes `shipyard.py` or `shipyard/`; if the companion is
still on its old process afterward, run `python3 shipyard-launchd.py restart`.
