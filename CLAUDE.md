# Shipyard

A focused dashboard for staying on top of GitHub pull requests — reviewer state, CI
status, stacked PRs, and the work needing your attention, in one place. Runs hosted on
GitHub Pages or locally with an optional companion process.

`AGENTS.md` is a symlink to this file, so Claude Code, Codex, and other agents read the
same guidance. Edit `CLAUDE.md`; never create a second copy.

## Architecture

- **`index.html`** — the entire frontend. Vanilla HTML + CSS + JS in one file, no
  framework, no bundler, no build step, no dependencies to install. Edit it directly.
- **`shipyard.py`** — optional local companion (Python 3 stdlib only). Serves the page
  and a `/worktrees.json` endpoint, and runs branch/worktree actions for local mode.
  Binds to localhost; discovery is read-only.
- **`shipyard.config.json`** — local companion config (scan roots, app launcher, branch
  prefix). Copy from `shipyard.config.example.json`.

Dashboard data and the GitHub token live in the browser's local storage; the token is
sent only to `api.github.com`.

## Running it

```bash
python3 shipyard.py ~/dev     # local mode: serve + match PRs to clones under ~/dev
open http://localhost:4321
```

Or serve the static page alone with `python3 -m http.server`. There is nothing to build
or install.

## Design system

Colors, fonts, and spacing tokens are CSS custom properties in the `:root` block at the
top of `index.html` (`--green`, `--fg-muted`, `--purple`, `--sans`, etc.). **Use these
variables — never hardcode a hex color or font stack.** When adding UI, match the weight
and size of nearby elements so new pieces don't outshout existing ones (e.g. row meta is
`11px/500-600`, the PR title is `15px/600`).

## Workflow

This is a solo indie project. **Commit and push directly to `main`** — no feature
branches, no PRs required. (This overrides any global "never commit on main" rule.)
Keep commits focused with a clear subject line.
