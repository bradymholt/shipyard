Shipyard (the PR dashboard) is asking for a re-review: the author changed this pull request after your earlier review. Do not edit files or post anything; this turn only reads and answers.

PR: {repo}#{number} "{title}" by {author} ({url})
Head now: {head}  Your last review was on: {reviewed_commit} ({review_state})

Your earlier review threads:
{threads}

Check each of your threads against the new commits (git diff {reviewed_commit}..HEAD where that commit exists, else `gh pr diff {number}`). Say for each whether it is addressed. Then decide whether the PR is ready to approve.

Answer with one JSON object and nothing else:
{{"summary": "N of M threads addressed; what is left",
  "addressed": 0, "total": 0,
  "proposals": [
    {{"action": "approve", "confidence": "high|medium|low", "reason": "why it is ready", "payload": {{"body": ""}}}},
    {{"action": "comment_unresolved", "confidence": "high|medium|low", "reason": "what is still open",
      "payload": {{"body": "a short comment listing what is still unaddressed"}}}}
  ]}}
Include only the proposals that fit.
