"""A small GitHub client for the companion, which makes every GitHub call for the page.
The token comes from `gh auth token` and never reaches the browser."""
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def gh_token():
    if not shutil.which("gh"):
        raise GitHubError("the GitHub CLI (gh) is not installed; install it and run `gh auth login`")
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitHubError(f"could not run `gh auth token`: {e}") from e
    token = r.stdout.strip()
    if r.returncode != 0 or not token:
        raise GitHubError((r.stderr or "`gh auth token` returned no token; run `gh auth login`").strip())
    return token


class GitHub:
    def __init__(self, token_source=gh_token, opener=urllib.request.urlopen):
        self._token_source = token_source
        self._token = None
        self._open = opener

    def token(self):
        if not self._token:
            self._token = self._token_source()
        return self._token

    def request(self, method, url, body=None, accept="application/vnd.github+json", raw=False,
                retry=None):
        """Reads retry on 5xx and network errors. Writes never do: a timeout after GitHub
        already applied one would post the reply, the review, or the merge twice."""
        data = json.dumps(body).encode() if body is not None else None
        retry = method == "GET" if retry is None else retry
        last = None
        for attempt in range(4 if retry else 1):
            if attempt:
                time.sleep(0.5 * 2 ** (attempt - 1))
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token()}",
                "Accept": accept,
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "shipyard-companion",
            })
            try:
                with self._open(req, timeout=30) as res:
                    payload = res.read()
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self._token = None  # re-read: `gh auth refresh` may have rotated it
                    raise GitHubError("GitHub rejected the gh token (401); run `gh auth login`", 401)
                if e.code in (502, 503, 504):
                    last = GitHubError(f"GitHub is temporarily unavailable ({e.code})", e.code)
                    continue
                detail = e.read().decode(errors="replace")[:300]
                raise GitHubError(f"GitHub {method} {url.replace(API, '')} failed: {e.code} {detail}", e.code)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = GitHubError(f"network error reaching GitHub: {e}")
                continue
            if raw:
                return payload
            return json.loads(payload) if payload else None
        raise last

    def graphql(self, query, variables=None):
        mutation = query.lstrip().startswith("mutation")
        out = self.request("POST", f"{API}/graphql", {"query": query, "variables": variables or {}},
                           retry=not mutation)
        if out.get("errors") and not any((out.get("data") or {}).values()):
            raise GitHubError("; ".join(e.get("message", "") for e in out["errors"]))
        return out["data"]

    def rest(self, method, path, body=None):
        return self.request(method, f"{API}{path}", body)

    def job_log_tail(self, repo, job_id, lines=150):
        """The end of a GitHub Actions job log, or "" when it is not available."""
        try:
            text = self.request("GET", f"{API}/repos/{repo}/actions/jobs/{job_id}/logs",
                                accept="application/vnd.github+json", raw=True).decode(errors="replace")
        except GitHubError:
            return ""
        return "\n".join(text.splitlines()[-lines:])
