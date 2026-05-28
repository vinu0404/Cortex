import importlib
import logging
import pkgutil
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def tool(
    description: str = "",
    requires_hitl: bool = False,
    connector: str = "",
):
    """Decorator that marks a function as a tool callable by agents."""
    def decorator(fn: Callable) -> Callable:
        fn.is_tool = True
        fn.tool_description = description or (fn.__doc__ or "")
        fn.requires_hitl = requires_hitl
        fn.connector = connector
        return fn
    return decorator


class ToolRegistry:
    _instance: "ToolRegistry | None" = None

    def __init__(self):
        self._tools: dict[str, Callable] = {}
        self._discovered = False

    @classmethod
    def get_instance(cls) -> "ToolRegistry":
        if cls._instance is None:
            cls._instance = cls()
            cls._instance.auto_discover()
        return cls._instance

    def auto_discover(self) -> None:
        if self._discovered:
            return
        import connectors as connectors_pkg
        for _, mod_name, _ in pkgutil.walk_packages(
            path=connectors_pkg.__path__,
            prefix=connectors_pkg.__name__ + ".",
            onerror=lambda x: None,
        ):
            if mod_name.endswith(".tools"):
                try:
                    mod = importlib.import_module(mod_name)
                    for attr_name in dir(mod):
                        obj = getattr(mod, attr_name)
                        if callable(obj) and getattr(obj, "is_tool", False):
                            self._tools[attr_name] = obj
                            logger.debug("Registered tool: %s", attr_name)
                except Exception as e:
                    logger.warning("Failed to load tools from %s: %s", mod_name, e)
        self._discovered = True
        logger.info("Registered %d tools", len(self._tools))

    def get_tool_schemas(self, tool_names: list[str]) -> list[dict]:
        schemas = []
        for name in tool_names:
            fn = self._tools.get(name)
            if fn:
                schemas.append({
                    "name": name,
                    "description": fn.tool_description,
                    "requires_hitl": fn.requires_hitl,
                    "connector": fn.connector,
                })
        return schemas

    def get_hitl_tools(self, tool_names: list[str]) -> list[str]:
        return [n for n in tool_names if getattr(self._tools.get(n), "requires_hitl", False)]

    def get_callable(self, tool_name: str) -> Callable | None:
        return self._tools.get(tool_name)

    def get_connector_tools(self, connector_name: str) -> dict[str, Callable]:
        return {
            name: fn
            for name, fn in self._tools.items()
            if getattr(fn, "connector", "") == connector_name
        }

    def all_tool_names(self) -> list[str]:
        return list(self._tools.keys())


def get_registry() -> ToolRegistry:
    return ToolRegistry.get_instance()
