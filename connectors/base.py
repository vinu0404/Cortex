from abc import ABC, abstractmethod


class BaseConnector(ABC):
    provider_name: str
    display_name: str
    auth_type: str = "oauth2"
    scopes: list[str] = []
    icon: str = ""

    @abstractmethod
    def get_auth_url(self, state: str) -> str:
        """Return OAuth2 authorization URL with state param."""

    @abstractmethod
    async def handle_callback(self, code: str, state: str) -> dict:
        """Exchange auth code for tokens. Return dict with access_token, refresh_token, expires_at, account_label."""

    @abstractmethod
    async def refresh_access_token(self, tokens: dict) -> dict:
        """Refresh expired access token. Return updated token dict."""

    def get_tool_definitions(self) -> list[dict]:
        """Return list of tool dicts: {name, description, requires_hitl}."""
        from tools.registry import get_registry
        registry = get_registry()
        return [
            {
                "name": name,
                "description": fn.__doc__ or "",
                "requires_hitl": getattr(fn, "requires_hitl", False),
            }
            for name, fn in registry.get_connector_tools(self.provider_name).items()
        ]
