import httpx

from config.settings import get_settings
from connectors.base import BaseConnector

settings = get_settings()

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GmailConnector(BaseConnector):
    provider_name = "gmail"
    display_name = "Gmail"
    auth_type = "oauth2"
    icon = "📧"
    scopes = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/userinfo.email",
    ]

    def get_auth_url(self, state: str) -> str:
        from urllib.parse import urlencode
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{_AUTH_URL}?{urlencode(params)}"

    async def handle_callback(self, code: str, state: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(_TOKEN_URL, data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            })
            resp.raise_for_status()
            data = resp.json()

        email = await self._fetch_user_email(data["access_token"])
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "expires_in": data.get("expires_in", 3600),
            "account_label": email,
        }

    async def refresh_access_token(self, tokens: dict) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(_TOKEN_URL, data={
                "refresh_token": tokens["refresh_token"],
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "grant_type": "refresh_token",
            })
            resp.raise_for_status()
            data = resp.json()

        tokens["access_token"] = data["access_token"]
        tokens["expires_in"] = data.get("expires_in", 3600)
        return tokens

    async def _fetch_user_email(self, access_token: str) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.googleapis.com/userinfo/v2/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 200:
                return resp.json().get("email", "gmail-user")
        return "gmail-user"
