import httpx

from config.settings import get_settings
from connectors.base import BaseConnector

settings = get_settings()

_AUTH_URL = "https://login.salesforce.com/services/oauth2/authorize"
_TOKEN_URL = "https://login.salesforce.com/services/oauth2/token"


class SalesforceConnector(BaseConnector):
    provider_name = "salesforce"
    display_name = "Salesforce"
    auth_type = "oauth2"
    icon = "☁️"
    scopes = ["api", "refresh_token", "offline_access"]

    def get_auth_url(self, state: str) -> str:
        return (
            f"{_AUTH_URL}?response_type=code"
            f"&client_id={settings.SALESFORCE_CLIENT_ID}"
            f"&redirect_uri={settings.SALESFORCE_REDIRECT_URI}"
            f"&scope={'%20'.join(self.scopes)}&state={state}"
        )

    async def handle_callback(self, code: str, state: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(_TOKEN_URL, data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.SALESFORCE_CLIENT_ID,
                "client_secret": settings.SALESFORCE_CLIENT_SECRET,
                "redirect_uri": settings.SALESFORCE_REDIRECT_URI,
            })
            resp.raise_for_status()
            data = resp.json()

        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "instance_url": data.get("instance_url", ""),
            "expires_in": 7200,
            "account_label": data.get("id", "salesforce-user"),
        }

    async def refresh_access_token(self, tokens: dict) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": settings.SALESFORCE_CLIENT_ID,
                "client_secret": settings.SALESFORCE_CLIENT_SECRET,
            })
            resp.raise_for_status()
            data = resp.json()
        tokens["access_token"] = data["access_token"]
        tokens["instance_url"] = data.get("instance_url", tokens.get("instance_url", ""))
        return tokens
