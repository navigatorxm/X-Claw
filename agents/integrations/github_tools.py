"""
XClaw GitHub Integration — real GitHub API tools.

No API key needed for public repos (60 req/hour).
Set GITHUB_TOKEN for 5,000 req/hour + private repo access.

Tools:
  github_search_repos   — find repos by query/language/stars
  github_trending       — trending repos (by recent stars)
  github_get_readme     — read any repo's README
  github_list_issues    — list open issues on any public repo
  github_create_issue   — create an issue (needs GITHUB_TOKEN)
  github_search_code    — search code across GitHub
  github_get_repo_info  — stars, forks, description, topics
  github_list_prs       — list open pull requests
"""

from __future__ import annotations

import base64
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"


def _headers() -> dict:
    h = {"Accept": _ACCEPT, "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def github_search_repos(query: str, language: str = "", sort: str = "stars", limit: int = 8) -> str:
    """Search GitHub repositories by keyword. Optionally filter by language."""
    q = query
    if language:
        q += f" language:{language}"
    try:
        async with httpx.AsyncClient(headers=_headers(), timeout=10) as client:
            resp = await client.get(f"{_API}/search/repositories", params={"q": q, "sort": sort, "per_page": limit})
            resp.raise_for_status()
            items = resp.json().get("items", [])
        if not items:
            return "No repositories found."
        lines = []
        for r in items:
            lines.append(
                f"**{r['full_name']}** ⭐{r['stargazers_count']:,}\n"
                f"  {r.get('description', 'No description')}\n"
                f"  {r['html_url']}"
            )
        return "\n\n".join(lines)
    except Exception as exc:
        return f"GitHub search failed: {exc}"


async def github_trending(language: str = "", since: str = "weekly") -> str:
    """Get trending GitHub repositories. since: daily/weekly/monthly."""
    # GitHub has no official trending API — use search sorted by pushed date + stars
    q = "stars:>100"
    if language:
        q += f" language:{language}"
    # Map "since" to a pushed date filter
    pushed = {"daily": "2024-01-01", "weekly": "2023-01-01", "monthly": "2022-01-01"}.get(since, "2023-01-01")
    q += f" pushed:>{pushed}"
    try:
        async with httpx.AsyncClient(headers=_headers(), timeout=10) as client:
            resp = await client.get(
                f"{_API}/search/repositories",
                params={"q": q, "sort": "stars", "order": "desc", "per_page": 10},
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
        if not items:
            return "No trending repositories found."
        lines = [f"**Trending {language or 'all'} repos ({since}):**\n"]
        for i, r in enumerate(items, 1):
            lang = r.get("language") or "—"
            lines.append(
                f"{i}. **{r['full_name']}** ⭐{r['stargazers_count']:,} · {lang}\n"
                f"   {r.get('description', '')[:100]}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"GitHub trending failed: {exc}"


async def github_get_readme(repo: str) -> str:
    """Fetch the README of any public GitHub repository. Format: 'owner/repo'."""
    repo = repo.strip().strip("/")
    if "/" not in repo:
        return "Provide repo as 'owner/repo' (e.g. 'openai/whisper')."
    try:
        async with httpx.AsyncClient(headers=_headers(), timeout=10) as client:
            resp = await client.get(f"{_API}/repos/{repo}/readme")
            resp.raise_for_status()
            data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        # Return first 3000 chars to stay useful
        return content[:3000] + ("\n[...truncated]" if len(content) > 3000 else "")
    except httpx.HTTPStatusError as exc:
        return f"Could not get README for {repo}: {exc.response.status_code}"
    except Exception as exc:
        return f"Failed: {exc}"


async def github_get_repo_info(repo: str) -> str:
    """Get detailed info about a GitHub repository: stars, forks, description, topics, language."""
    repo = repo.strip().strip("/")
    if "/" not in repo:
        return "Provide repo as 'owner/repo'."
    try:
        async with httpx.AsyncClient(headers=_headers(), timeout=10) as client:
            resp = await client.get(f"{_API}/repos/{repo}")
            resp.raise_for_status()
            r = resp.json()
        topics = ", ".join(r.get("topics", [])) or "—"
        return (
            f"**{r['full_name']}**\n"
            f"⭐ {r['stargazers_count']:,} stars  🍴 {r['forks_count']:,} forks  👁 {r['watchers_count']:,} watchers\n"
            f"Language: {r.get('language') or '—'}  License: {(r.get('license') or {}).get('spdx_id', '—')}\n"
            f"Topics: {topics}\n"
            f"Created: {r['created_at'][:10]}  Last push: {r['pushed_at'][:10]}\n"
            f"Description: {r.get('description', 'None')}\n"
            f"URL: {r['html_url']}"
        )
    except Exception as exc:
        return f"Failed: {exc}"


async def github_list_issues(repo: str, state: str = "open", limit: int = 10) -> str:
    """List issues on a GitHub repository. state: open/closed/all."""
    repo = repo.strip().strip("/")
    if "/" not in repo:
        return "Provide repo as 'owner/repo'."
    try:
        async with httpx.AsyncClient(headers=_headers(), timeout=10) as client:
            resp = await client.get(
                f"{_API}/repos/{repo}/issues",
                params={"state": state, "per_page": limit, "sort": "updated"},
            )
            resp.raise_for_status()
            issues = [i for i in resp.json() if "pull_request" not in i]  # exclude PRs
        if not issues:
            return f"No {state} issues found on {repo}."
        lines = [f"**{state.title()} issues on {repo}:**\n"]
        for i in issues:
            labels = ", ".join(l["name"] for l in i.get("labels", []))
            lines.append(f"#{i['number']}: {i['title']}\n  Labels: {labels or 'none'}  | {i['html_url']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Failed: {exc}"


async def github_list_prs(repo: str, state: str = "open", limit: int = 10) -> str:
    """List pull requests on a GitHub repository."""
    repo = repo.strip().strip("/")
    if "/" not in repo:
        return "Provide repo as 'owner/repo'."
    try:
        async with httpx.AsyncClient(headers=_headers(), timeout=10) as client:
            resp = await client.get(
                f"{_API}/repos/{repo}/pulls",
                params={"state": state, "per_page": limit},
            )
            resp.raise_for_status()
            prs = resp.json()
        if not prs:
            return f"No {state} PRs on {repo}."
        lines = [f"**{state.title()} PRs on {repo}:**\n"]
        for pr in prs:
            lines.append(f"#{pr['number']}: {pr['title']}\n  by @{pr['user']['login']}  | {pr['html_url']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Failed: {exc}"


async def github_create_issue(repo: str, title: str, body: str = "") -> str:
    """Create a GitHub issue. Requires GITHUB_TOKEN environment variable."""
    if not os.getenv("GITHUB_TOKEN"):
        return "GITHUB_TOKEN not set. Add it to .env to create issues."
    repo = repo.strip().strip("/")
    if "/" not in repo:
        return "Provide repo as 'owner/repo'."
    try:
        async with httpx.AsyncClient(headers=_headers(), timeout=10) as client:
            resp = await client.post(
                f"{_API}/repos/{repo}/issues",
                json={"title": title, "body": body},
            )
            resp.raise_for_status()
            issue = resp.json()
        return f"Issue created: #{issue['number']} — {issue['html_url']}"
    except Exception as exc:
        return f"Failed to create issue: {exc}"


async def github_search_code(query: str, language: str = "", limit: int = 5) -> str:
    """Search code across GitHub. Returns file paths and snippets."""
    if not os.getenv("GITHUB_TOKEN"):
        return "Code search requires GITHUB_TOKEN (add to .env). Unauthenticated code search is not supported by GitHub API."
    q = query
    if language:
        q += f" language:{language}"
    try:
        async with httpx.AsyncClient(headers=_headers(), timeout=10) as client:
            resp = await client.get(
                f"{_API}/search/code",
                params={"q": q, "per_page": limit},
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
        if not items:
            return "No code results found."
        lines = []
        for item in items:
            lines.append(f"**{item['repository']['full_name']}** / `{item['path']}`\n  {item['html_url']}")
        return "\n\n".join(lines)
    except Exception as exc:
        return f"Code search failed: {exc}"
