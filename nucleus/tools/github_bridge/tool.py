from __future__ import annotations

import json, os, sys, urllib.request, urllib.error, base64
from pathlib import Path

GITHUB_API = "https://api.github.com"


def _headers() -> dict:
    h = {"Accept": "application/vnd.github.v3+json", "User-Agent": "Ouroboros"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _request(method: str, url: str, data: dict | None = None) -> dict:
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}", "detail": e.read().decode()[:500]}


def list_repos() -> dict:
    return _request("GET", f"{GITHUB_API}/user/repos?per_page=30&sort=updated")


def create_repo(name: str, private: bool = True) -> dict:
    return _request("POST", f"{GITHUB_API}/user/repos", {"name": name, "private": private})


def list_issues(repo: str) -> dict:
    return _request("GET", f"{GITHUB_API}/repos/{repo}/issues?state=open&per_page=20")


def create_issue(repo: str, title: str, body: str = "") -> dict:
    return _request("POST", f"{GITHUB_API}/repos/{repo}/issues", {"title": title, "body": body})


def get_readme(repo: str) -> dict:
    result = _request("GET", f"{GITHUB_API}/repos/{repo}/readme")
    if "content" in result:
        result["decoded"] = base64.b64decode(result["content"]).decode("utf-8", errors="replace")
    return result


def main() -> None:
    args = json.loads(sys.stdin.read() or "{}")
    action = str(args.get("action", ""))
    repo = str(args.get("repo", ""))
    try:
        if action == "list_repos":
            result = list_repos()
        elif action == "create_repo":
            result = create_repo(str(args.get("name", "")))
        elif action == "list_issues":
            result = list_issues(repo)
        elif action == "create_issue":
            result = create_issue(repo, str(args.get("title", "")), str(args.get("body", "")))
        elif action == "get_readme":
            result = get_readme(repo)
        else:
            result = {"error": f"Unknown action: {action}"}
    except Exception as e:
        result = {"error": str(e)}
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
