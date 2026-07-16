# Shipyard

Shipyard is a focused dashboard for staying on top of GitHub pull requests. It brings reviewer state, CI status, stacked PRs, and the work that needs your attention into one place.

[Try Shipyard in your browser](https://bradymholt.github.io/shipyard/)

## Screenshots

### List view

Prioritize PRs with reviewer, review, and CI status visible together.

![Shipyard list view](docs/list-view.png)

### Board view

See drafts, waiting work, approvals, and recent merges by stage.

![Shipyard board view](docs/board-view.png)

## Quick start

### Hosted

Open the [hosted dashboard](https://bradymholt.github.io/shipyard/), click the gear icon, add a GitHub token, then add a repository, organization, or username to your views. Nothing needs to be installed.

### With local integration

The optional local companion adds VS Code links and branch/worktree actions. It requires Git and Python 3; VS Code is only needed for the editor links.

```bash
git clone https://github.com/bradymholt/shipyard.git
cd shipyard
python3 index.py ~/dev
```

Replace `~/dev` with a folder that directly contains your Git clones, then open the URL printed by the command. You can pass more than one folder or use `--port` to choose another port.

## GitHub token

A fine-grained personal access token limited to the repositories you use is recommended. Pull requests read access is enough for viewing; write access is needed to toggle auto-merge or mark a draft ready for review. A classic token needs the `repo` scope for private repositories.

Your token stays in your browser's local storage and is sent only to `api.github.com`. The hosted and local versions use separate browser storage, so each needs to be configured once.

## Features

- **Action-focused views:** My PRs, Waiting on my review, and All open, with priority, updated, and created sorting.
- **List and board layouts:** reviewer state, drafts, approvals, and ready-to-merge work at a glance.
- **Useful context:** CI checks, labels, comments, stacked PR relationships, and your two most recently merged PRs.
- **Flexible scope:** combine repositories, organizations, and usernames, then filter across titles, authors, branches, and repositories.
- **PR actions:** toggle auto-merge and move drafts to ready without leaving the dashboard.
- **Optional local workflow:** open existing checkouts in VS Code or start a branch in the main clone or an isolated worktree.

## Local companion

`index.py` serves the dashboard locally and matches GitHub PR branches to clones found under your configured folders. It binds to localhost and accepts branch actions only from the dashboard it serves.

Worktree discovery is read-only. Actions you choose can fetch, create, or switch local branches and create worktrees. Shipyard refuses to switch a main clone with tracked changes, opens an existing checkout when one already owns the branch, and never removes existing worktrees. New worktrees are created under `<repo>/.claude/worktrees/`.

For a persistent setup, copy the example configuration and adjust `roots`:

```bash
cp shipyard.config.example.json shipyard.config.json
python3 index.py
```

```json
{
  "roots": ["~/dev"],
  "vscodeOpen": "scheme",
  "codeCliArgs": [],
  "branchPrefix": ""
}
```

The default `scheme` setting uses `vscode://` links. Set it to `cli` if you prefer the companion to run the `code` command; `codeCliArgs` can supply extra CLI arguments. `branchPrefix` optionally prefills new branch names.

## Development

There is no build step or dependency installation. Serve `index.html` with `python3 -m http.server`, or run `python3 index.py ~/dev` to exercise the local integration. Issues and pull requests are welcome.

## License

Shipyard is available under the [MIT License](LICENSE).
