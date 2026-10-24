Shipyard (the PR dashboard) is asking for a re-review: the author changed this pull request after your earlier review. Do not edit files or post anything; this turn only reads and answers.

PR: {repo}#{number} "{title}" by {author} ({url})
Head now: {head}  Your last review was on: {reviewed_commit} ({review_state})

Your earlier review threads:
{threads}

Check each of your threads against the code as it is now (this checkout is at the PR's head; `gh pr diff {number}` shows the whole change, `gh pr view {number} --json commits` the commits since). Say for each whether it is addressed.

Then review everything that changed since your last review: `git diff {reviewed_commit} {head}` (fetch first if the commit is missing). Flag anything that goes beyond answering your threads: new behavior, files unrelated to the threads, risky edits (auth, data migrations, deletes, config, dependencies). Name each in the summary. If anything is flagged, don't propose `approve` with high confidence; use medium or low so it waits for you.

Then decide whether the PR is ready to approve.

Answer with one JSON object and nothing else:
{{"summary": "N of M threads addressed; what is left",
  "addressed": 0, "total": 0,
  "proposals": [
    {{"action": "approve", "confidence": "high|medium|low", "reason": "why it is ready", "payload": {{"body": ""}}}},
    {{"action": "comment_unresolved", "confidence": "high|medium|low", "reason": "what is still open",
      "payload": {{"body": "a short comment listing what is still unaddressed"}}}}
  ]}}
Include only the proposals that fit.
