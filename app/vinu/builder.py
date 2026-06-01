import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

from app.auth.db_models import User
from app.connectors.manager import get_auth_free_connector_slugs, get_connector_display
from app.workspaces.db_models import Workspace
from config.settings import get_settings
from database.session import get_custom_db_context_session

settings = get_settings()
logger = logging.getLogger(__name__)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@dataclass
class _AgentBuildContext:
    workspace_id: UUID
    user_id: UUID
    kb_name_to_id: dict[str, UUID]
    wc_name_to_id: dict[str, UUID]
    tool_connector_map: dict[str, str]
    default_model_id: str
    api_key_id: UUID | None


def _resolve_agent_model(spec_model: str | None, default_model_id: str) -> str:
    """Return spec model if it's a real model name, otherwise fall back to default."""
    if not spec_model or spec_model == "default":
        return default_model_id
    return spec_model


def _build_tool_connector_map() -> dict[str, str]:
    from tools.registry import get_registry
    registry = get_registry()
    return {s["name"]: s["connector"] for s in registry.get_tool_schemas(registry.all_tool_names())}


def _collect_connectors_needed(plan: dict, tool_connector_map: dict[str, str]) -> list[dict]:
    auth_free = get_auth_free_connector_slugs()
    seen: set[str] = set()
    for agent_spec in plan.get("agents", []):
        for tool_name in agent_spec.get("tools", []):
            slug = tool_connector_map.get(tool_name, "")
            if slug and slug not in auth_free:
                seen.add(slug)
    return [{"slug": slug, **get_connector_display(slug)} for slug in sorted(seen)]


def _collect_agents_summary(plan: dict) -> list[dict]:
    return [
        {
            "name": a.get("name", ""),
            "role": a.get("role", ""),
            "tools": a.get("tools", []),
            "kb_names": a.get("kb_names", []),
            "wc_names": a.get("wc_names", []),
        }
        for a in plan.get("agents", [])
    ]


def _collect_done_payload(
    plan: dict,
    ws: Workspace,
    kb_name_to_id: dict[str, UUID],
    wc_name_to_id: dict[str, UUID],
    tool_connector_map: dict[str, str],
) -> dict:
    kbs_created = [
        {"name": s["name"], "id": str(kb_name_to_id[s["name"]]), "description": s.get("description", "")}
        for s in plan.get("kbs_needed", []) if s["name"] in kb_name_to_id
    ]
    wcs_created = [
        {"name": s["name"], "id": str(wc_name_to_id[s["name"]]), "description": s.get("description", "")}
        for s in plan.get("wcs_needed", []) if s["name"] in wc_name_to_id
    ]
    return {
        "workspace_id": str(ws.id),
        "workspace_name": plan.get("workspace_name", ""),
        "message": "Your workspace is ready!",
        "kbs_created": kbs_created,
        "wcs_created": wcs_created,
        "connectors_needed": _collect_connectors_needed(plan, tool_connector_map),
        "agents_summary": _collect_agents_summary(plan),
    }


async def build_workspace(user: User, plan: dict) -> AsyncGenerator[str, None]:
    tool_connector_map = _build_tool_connector_map()
    try:
        model_id, api_key_id = await _resolve_api_key(user)
        ws_name = plan.get("workspace_name", "My Workspace")
        ws_desc = plan.get("workspace_description", "")

        yield _sse("step", {"step": f"Creating workspace \"{ws_name}\"…", "status": "running"})
        if await _workspace_name_exists(user.id, ws_name):
            yield _sse("error", {"message": f'A workspace named "{ws_name}" already exists. Ask Vinu to use a different name.'})
            return
        ws = await _create_workspace(user.id, ws_name, ws_desc)
        yield _sse("step", {"step": f"Workspace \"{ws_name}\" created", "status": "done", "result": {"id": str(ws.id)}})

        kb_name_to_id: dict[str, UUID] = {}
        async for event in _create_kbs(plan.get("kbs_needed", []), user.id, kb_name_to_id):
            yield event

        wc_name_to_id: dict[str, UUID] = {}
        async for event in _create_wcs(plan.get("wcs_needed", []), user.id, wc_name_to_id):
            yield event

        ctx = _AgentBuildContext(
            workspace_id=ws.id, user_id=user.id,
            kb_name_to_id=kb_name_to_id, wc_name_to_id=wc_name_to_id,
            tool_connector_map=tool_connector_map, default_model_id=model_id,
            api_key_id=api_key_id,
        )
        async for event in _create_agents(plan.get("agents", []), ctx):
            yield event

        yield _sse("done", _collect_done_payload(plan, ws, kb_name_to_id, wc_name_to_id, tool_connector_map))

    except Exception as exc:
        logger.error("Vinu build failed: %s", exc, exc_info=True)
        yield _sse("error", {"message": str(exc)})


async def _resolve_api_key(user: User) -> tuple[str, UUID | None]:
    from app.api_keys.manager import ApiKeyManager

    async with get_custom_db_context_session() as db:
        keys = await ApiKeyManager(db).list_keys(user.id)
        for key in keys:
            if key.available_models:
                return key.available_models[0], key.id
    return settings.DEFAULT_MODEL, None


async def _workspace_name_exists(user_id: UUID, name: str) -> bool:
    from app.workspaces.manager import WorkspaceManager

    async with get_custom_db_context_session() as db:
        return await WorkspaceManager(db).find_by_name(user_id, name) is not None


async def _create_workspace(user_id: UUID, name: str, description: str) -> Workspace:
    from app.workspaces.manager import WorkspaceManager

    async with get_custom_db_context_session() as db:
        return await WorkspaceManager(db).create_workspace(user_id, name, description)


async def _create_kbs(
    plan_kbs: list[dict],
    user_id: UUID,
    kb_name_to_id: dict[str, UUID],
) -> AsyncGenerator[str, None]:
    """Yields SSE step events and populates kb_name_to_id as an out-param."""
    from app.knowledge_bases.manager import KnowledgeBaseManager

    async with get_custom_db_context_session() as db:
        mgr = KnowledgeBaseManager(db)
        existing_kbs = await mgr.list_kbs(user_id)
        existing_kb_map = {kb.name: kb for kb in existing_kbs}

        for spec in plan_kbs:
            name = spec["name"]
            if name in existing_kb_map:
                existing_kb = existing_kb_map[name]
                kb_name_to_id[name] = existing_kb.id
                yield _sse("step", {
                    "step": f"Knowledge base \"{name}\" already exists — reusing it",
                    "status": "done",
                    "result": {"id": str(existing_kb.id)},
                })
            else:
                yield _sse("step", {"step": f"Creating knowledge base \"{name}\"…", "status": "running"})
                kb = await mgr.create_kb(user_id, name, spec.get("description", ""))
                await db.commit()
                kb_name_to_id[name] = kb.id
                yield _sse("step", {
                    "step": f"Knowledge base \"{name}\" created (upload docs to populate)",
                    "status": "done",
                    "result": {"id": str(kb.id)},
                })

        for kb in existing_kbs:
            if kb.name not in kb_name_to_id:
                kb_name_to_id[kb.name] = kb.id


async def _create_wcs(
    plan_wcs: list[dict],
    user_id: UUID,
    wc_name_to_id: dict[str, UUID],
) -> AsyncGenerator[str, None]:
    """Yields SSE step events and populates wc_name_to_id as an out-param."""
    from app.website_collections.manager import WebsiteCollectionManager

    async with get_custom_db_context_session() as db:
        mgr = WebsiteCollectionManager(db)
        existing_wcs = await mgr.list_collections(user_id)
        existing_wc_map = {wc.name: wc for wc in existing_wcs}

        for spec in plan_wcs:
            name = spec["name"]
            if name in existing_wc_map:
                existing_wc = existing_wc_map[name]
                wc_name_to_id[name] = existing_wc.id
                yield _sse("step", {
                    "step": f"Website collection \"{name}\" already exists — reusing it",
                    "status": "done",
                    "result": {"id": str(existing_wc.id)},
                })
            else:
                url = spec.get("url", "")
                yield _sse("step", {"step": f"Creating website collection \"{name}\"…", "status": "running"})
                wc = await mgr.create_collection(user_id, name, spec.get("description", ""))
                await db.commit()
                wc_id = wc.id  # capture before commit expiry
                wc_name_to_id[name] = wc_id
                async for event in _maybe_scrape_wc(mgr, wc_id, user_id, name, url):
                    yield event

        for wc in existing_wcs:
            if wc.name not in wc_name_to_id:
                wc_name_to_id[wc.name] = wc.id


async def _maybe_scrape_wc(mgr, wc_id: UUID, user_id: UUID, name: str, url: str) -> AsyncGenerator[str, None]:
    if url:
        wu = await mgr.add_url(wc_id, user_id, url, max_depth=2)
        await mgr.trigger_scrape(wc_id, wu.id, user_id)
        yield _sse("step", {
            "step": f"Website collection \"{name}\" created, scraping {url}…",
            "status": "done",
            "result": {"id": str(wc_id)},
        })
    else:
        yield _sse("step", {
            "step": f"Website collection \"{name}\" created",
            "status": "done",
            "result": {"id": str(wc_id)},
        })


async def _create_agents(
    plan_agents: list[dict],
    ctx: _AgentBuildContext,
) -> AsyncGenerator[str, None]:
    from app.agents.manager import AgentManager

    for order, spec in enumerate(plan_agents, start=1):
        name = spec.get("name", f"Agent {order}")
        yield _sse("step", {"step": f"Creating agent \"{name}\"…", "status": "running"})

        kb_ids = [ctx.kb_name_to_id[n] for n in spec.get("kb_names", []) if n in ctx.kb_name_to_id]
        collection_ids = [ctx.wc_name_to_id[n] for n in spec.get("wc_names", []) if n in ctx.wc_name_to_id]
        tools_config = [
            {"tool": t, "connector_slug": ctx.tool_connector_map.get(t, t), "requires_hitl": False}
            for t in spec.get("tools", [])
        ]

        async with get_custom_db_context_session() as db:
            agent = await AgentManager(db).create_agent(
                workspace_id=ctx.workspace_id,
                user_id=ctx.user_id,
                name=name,
                system_prompt=spec.get("system_prompt", ""),
                model_id=_resolve_agent_model(spec.get("model"), ctx.default_model_id),
                api_key_id=ctx.api_key_id,
                display_order=order,
                tools_config=tools_config,
                kb_ids=kb_ids or None,
                collection_ids=collection_ids or None,
            )

        yield _sse("step", {
            "step": f"Agent \"{name}\" created",
            "status": "done",
            "result": {"id": str(agent.id)},
        })
