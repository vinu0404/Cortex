import httpx

from app.common.retry import async_http_request_with_retry
from config.settings import get_settings
from connectors.base import BaseConnector

settings = get_settings()

_AUTH_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"


class GitHubConnector(BaseConnector):
    provider_name = "github"
    display_name = "GitHub"
    auth_type = "oauth2"
    icon = "🐙"
    scopes = ["repo", "read:user", "user:email", "issues:write"]

    def get_auth_url(self, state: str) -> str:
        scope = " ".join(self.scopes)
        return (
            f"{_AUTH_URL}?client_id={settings.GITHUB_CLIENT_ID}"
            f"&redirect_uri={settings.GITHUB_REDIRECT_URI}"
            f"&scope={scope}&state={state}"
        )

    async def handle_callback(self, code: str, state: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await async_http_request_with_retry(
                client,
                "POST",
                _TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": settings.GITHUB_CLIENT_ID,
                    "client_secret": settings.GITHUB_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": settings.GITHUB_REDIRECT_URI,
                },
            )
            data = resp.json()

        username = await self._fetch_username(data["access_token"])
        return {
            "access_token": data["access_token"],
            "refresh_token": "",
            "expires_in": 0,
            "account_label": username,
        }

    async def refresh_access_token(self, tokens: dict) -> dict:
        # GitHub tokens don't expire (unless OAuth app uses expiring tokens)
        return tokens

    async def _fetch_username(self, access_token: str) -> str:
        async with httpx.AsyncClient() as client:
            resp = await async_http_request_with_retry(
                client,
                "GET",
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            )
            return resp.json().get("login", "github-user")
        return "github-user"
