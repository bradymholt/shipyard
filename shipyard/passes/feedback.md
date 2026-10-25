Shipyard (the PR dashboard) is checking in on this pull request because a reviewer requested changes or left review comments. Do not edit files or run commands that change anything; this turn only reads and answers.

PR: {repo}#{number} "{title}" ({url})
Branch: {branch} at {head}

Review feedback (unresolved threads first; each has a comment_id you can reply to; a review's own text has none, so a reply to it leaves comment_id out and lands on the PR):
{feedback}

For each piece of feedback, say in one line what the reviewer is asking for. Then give a short plan for addressing all of it. Where a reply is the whole answer (a question, a misunderstanding, a deliberate choice), draft that reply in the voice of the PR author: brief, specific, no greeting.

If the feedback is long, read files or the diff with a sub-agent so only its short result lands here.

Answer with one JSON object and nothing else:
{{"summary": "2-4 sentences: what they want and the plan",
  "proposals": [
    {{"action": "continue", "confidence": "high", "reason": "the plan needs code changes", "payload": {{}}}},
    {{"action": "post_reply", "confidence": "high|medium|low", "reason": "why a reply answers it",
      "payload": {{"comment_id": 123, "body": "the drafted reply"}}}}
  ]}}
Use "post_reply" only where a reply is the answer. Keep it terse; this lands in the PR's own session.
