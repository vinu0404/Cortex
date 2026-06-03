"""GitHub tool functions."""
from app.common.retry import async_http_request_with_retry
from tools.registry import tool


@tool(description="List GitHub repositories for the authenticated user", connector="github")
async def github_list_repos(access_token: str, per_page: int = 10) -> dict:
    """List GitHub repositories."""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "GET",
            "https://api.github.com/user/repos",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            params={"per_page": per_page, "sort": "updated"},
        )
    repos = [{"name": r["name"], "full_name": r["full_name"], "description": r["description"], "url": r["html_url"]} for r in resp.json()]
    return {"repos": repos}


@tool(description="List open issues in a GitHub repository", connector="github")
async def github_list_issues(access_token: str, repo: str, state: str = "open", per_page: int = 10) -> dict:
    """List issues for a repo. repo format: owner/repo"""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "GET",
            f"https://api.github.com/repos/{repo}/issues",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            params={"state": state, "per_page": per_page},
        )
    issues = [{"number": i["number"], "title": i["title"], "state": i["state"], "url": i["html_url"]} for i in resp.json()]
    return {"issues": issues}


@tool(description="Create a GitHub issue", requires_hitl=True, connector="github")
async def github_create_issue(access_token: str, repo: str, title: str, body: str = "", labels: list[str] | None = None) -> dict:
    """Create a new issue in a GitHub repository. Requires HITL approval."""
    import httpx
    payload: dict = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "POST",
            f"https://api.github.com/repos/{repo}/issues",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            json=payload,
        )
    data = resp.json()
    return {"issue_number": data["number"], "url": data["html_url"], "created": True}


@tool(description="Get pull requests for a repository", connector="github")
async def github_list_pull_requests(access_token: str, repo: str, state: str = "open") -> dict:
    """List pull requests for a repo."""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "GET",
            f"https://api.github.com/repos/{repo}/pulls",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            params={"state": state, "per_page": 20},
        )
    prs = [{"number": p["number"], "title": p["title"], "state": p["state"], "url": p["html_url"]} for p in resp.json()]
    return {"pull_requests": prs}
