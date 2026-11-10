Shipyard (the PR dashboard) has merged {base} into this pull request's branch in a scratch checkout, and the merge conflicts. Do not edit files or run commands that change anything; this turn only reads and answers. Shipyard writes your resolutions into the files and shows them to the user before anything is pushed.

PR: {repo}#{number} "{title}" ({url})
Branch: {branch} at {head}, merging in {base}

The working directory is that checkout, mid-merge. These files have conflict blocks, marked the usual way (`<<<<<<< HEAD` is this PR's side, `>>>>>>> origin/{base}` is {base}'s side):
{conflicts}

Read each conflicted file and work out what both sides meant. `gh pr diff {number}` shows the PR's intent, and `git diff HEAD...MERGE_HEAD -- <file>` shows what {base} changed. Resolve each block so both changes survive; drop a side only when the other replaces it.

{instructions}Answer with one JSON object and nothing else:
{{"summary": "2-3 sentences: what conflicted and how you resolved it",
  "proposals": [
    {{"action": "push_merge", "confidence": "high|medium|low", "reason": "how the resolution keeps both changes",
      "payload": {{"files": [{{"path": "a conflicted file", "resolutions": ["the exact text that replaces block 1, without markers", "block 2", "..."]}}]}}}},
    {{"action": "continue", "confidence": "high", "reason": "why this needs a person: the sides contradict, or the right answer depends on intent", "payload": {{}}}}
  ]}}
A push_merge must give every file above exactly as many resolutions as it has blocks, in order. Include only the proposals that fit.
