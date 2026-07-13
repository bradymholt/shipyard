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
- **List view**: GitHub-style list with review status, current reviewers (avatars with per-reviewer state), CI check status, labels, and comment counts
- **Board view**: swim lanes for Working / Waiting / Ready on the My PRs tab, with drafts styled distinctly in Working
- **Stacked PRs**: PRs based on another open PR's branch are nested under their parent in both views
- **Priority sort**: ready-to-merge first, then actionable-by-author, then needs-reviewer, awaiting-review last
- **Filters**: My PRs / Waiting on my review / All open tabs, plus text filter across title, author, branch, and repo

## Setup

Open the page, click the gear icon, and paste a GitHub personal access token - classic with `repo` scope, or fine-grained with Pull requests: read access. The token is stored only in your browser's localStorage and is sent only to `api.github.com`.

## Development

No build step. Edit `index.html`, serve it locally (`python3 -m http.server`), and refresh.
