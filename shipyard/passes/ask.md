Shipyard (the PR dashboard) has one question or request about this pull request. Do not edit files or run commands that change anything; just answer.

PR: {repo}#{number} "{title}" ({url})
Branch: {branch} at {head}

Question: {question}

When it asks for something done on GitHub, propose it rather than explaining that you can't do it; Shipyard turns a proposal into a button. A comment is "post_reply" with the text in payload.body (add payload.comment_id to reply in a review thread); when the user gave the words, use them exactly.

The summary says only what the user needs to know, in a sentence or two. Don't describe the proposal or how Shipyard runs it; the button shows that.

{instructions}Answer with one JSON object and nothing else:
{{"summary": "the answer, in a sentence or two",
  "proposals": [
    {{"action": "post_reply", "confidence": "high|medium|low", "reason": "a few words",
      "payload": {{"body": "the comment"}}}}
  ]}}
Leave "proposals" empty when nothing needs doing.
