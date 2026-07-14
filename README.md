# PR Review Dashboard

A GitHub pull request dashboard, hosted on GitHub Pages. It shows open PRs with reviewer and review-status info that GitHub's default list view doesn't surface.

**Live:** https://bradymholt.github.io/pr-review-dashboard/

## Screenshots

### List view

Priority-sorted list with review status, reviewers, CI checks, and stacked PRs nested under their parent:

![List view](docs/list-view.png)

### Board view

Swim lanes by PR state - Working (drafts shown dashed), Waiting (on a reviewer), and Ready (approved):

![Board view](docs/board-view.png)

## Features

- **Views**: add any mix of repos (`owner/repo`), orgs, or usernames and switch between them
- **List view**: GitHub-style list with review status, CI check status, labels, and comment counts. Shows reviewer avatars (with per-reviewer state) on your PRs, and the author's avatar on "Waiting on my review" so you can see whose PR needs you
- **Sort**: list views sort by priority (default), recently updated, or recently created
- **Board view**: swim lanes for Working / Waiting / Ready on the My PRs tab, with drafts styled distinctly in Working (click a card's Draft pill to mark it ready for review)
- **Auto-merge**: Waiting and Ready cards show auto-merge status with a one-click toggle
- **Stacked PRs**: PRs based on another open PR's branch are nested under their parent in both views
- **Priority sort**: one of the list sort options — ready-to-merge first, then actionable-by-author, then needs-reviewer, awaiting-review last
- **Filters**: My PRs / Waiting on my review / All open tabs, plus text filter across title, author, branch, and repo. The default load fetches only your PRs and ones awaiting your review; "All open" fetches the full set on demand the first time you open it
- **Local worktree links** (optional): when run via the local companion, PRs whose branch you have checked out locally get "Open in VS Code" and "Resume in Claude" links

## Setup

Open the page, click the gear icon, and paste a GitHub personal access token - classic with `repo` scope, or fine-grained with Pull requests: read access. The token is stored only in your browser's localStorage and is sent only to `api.github.com`.

## Local companion (optional)

`companion.py` is a small local server that adds "Open in VS Code" / "Resume in Claude" links for branches you have checked out as local git worktrees. It reads `git worktree list` (and Claude Code's session files under `~/.claude`) and serves both the dashboard and a `/worktrees.json` endpoint.

```
python3 companion.py ~/dev        # scan git repos under ~/dev
```

Then open the printed `http://localhost:4321`. Pass multiple roots or `--port` as needed. It binds to localhost only, matches worktrees to PRs by the repo's `origin` remote + branch, and never writes anything to the repo. The hosted GitHub Pages copy doesn't reach the companion (browsers block HTTPS→localhost), so the worktree links only appear when you're viewing the dashboard through the companion.

The "Open in VS Code" button asks the companion to run `code`, which opens a `.code-workspace` file in the worktree as a workspace when one exists (otherwise the folder). Requires the `code` CLI on your PATH.

Note: the companion serves the dashboard on a different origin (`localhost`) than Pages, so localStorage (token, views) is separate there - you enter the token once for the local origin.

## Development

No build step. Edit `index.html`, serve it locally (`python3 -m http.server`), and refresh. See `companion.py` for the optional worktree integration.
