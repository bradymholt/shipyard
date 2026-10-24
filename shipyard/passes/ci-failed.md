Shipyard (the PR dashboard) is asking about a CI failure on this pull request. Do not edit files or run commands that change anything; this turn only reads and answers.

PR: {repo}#{number} "{title}" ({url})
Branch: {branch} at {head}
Failing checks:
{checks}

End of the failing job log(s):
{log}

Find the likely cause. Compare against the change in this branch (git diff origin/{base}...HEAD) where that helps. If the log is long, read it with a sub-agent so only its short result lands here. Decide whether the failure looks caused by this PR or flaky/unrelated (a network error, a timeout in code the PR doesn't touch, a known-flaky test).

Answer with one JSON object and nothing else:
{{"summary": "2-3 sentences: what failed and why",
  "proposals": [
    {{"action": "rerun_checks", "confidence": "high|medium|low", "reason": "only if it looks flaky; say why", "payload": {{}}}},
    {{"action": "apply_fix", "confidence": "high|medium|low", "reason": "what the fix does",
      "payload": {{"diff": "a unified diff against HEAD, only when the fix is small and certain"}}}},
    {{"action": "continue", "confidence": "high", "reason": "the fix needs real work", "payload": {{}}}}
  ]}}
Include only the proposals that fit.
