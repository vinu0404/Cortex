import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import litellm
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.common.token_utils import TokenUsage, calculate_usage
from config.settings import get_settings
from core.schemas import AgentInput, AgentOutput, Source
from tools.registry import get_registry

settings = get_settings()
logger = logging.getLogger(__name__)


def _is_retriable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "rate" in msg or "timeout" in msg or "connection" in msg or "500" in msg or "503" in msg


def _build_system_prompt(agent_def: dict, agent_input: AgentInput, is_embed: bool = False) -> str:
    parts = []
    if agent_def.get("system_prompt"):
        parts.append(agent_def["system_prompt"])

    persona = agent_input.metadata.get("persona")
    if persona:
        parts.append(f"\n## Persona\n{persona}")

    if agent_input.long_term_memory.critical_facts:
        facts = {k: v for k, v in agent_input.long_term_memory.critical_facts.items() if v not in (None, "", [], {})}
        if facts:
            parts.append(
                f"\n## User Context\n"
                f"You know these facts about the user. Use them when answering questions about the user — "
                f"do NOT say you don't know if the answer is present here.\n"
                f"{json.dumps(facts)}"
            )

    if agent_input.dependency_outputs:
        parts.append("\n## Outputs from upstream agents:")
        for dep_id, data in agent_input.dependency_outputs.items():
            parts.append(f"- {dep_id}: {json.dumps(data)[:500]}")

    if agent_input.hitl_context and agent_input.hitl_context.approved:
        if agent_input.hitl_context.instructions:
            parts.append(f"\n## Human Instructions for Tool Use\n{agent_input.hitl_context.instructions}")

    if is_embed:
        parts.append(
            "\n\n## Embed Context\n"
            "This conversation comes from an embedded chatbot on an external website. "
            "Prioritise answering from knowledge bases and website collections before reaching for external tools. "
            "Search KB/WC thoroughly. Give complete, self-contained answers — the visitor has no other context."
        )

    tz_name = agent_input.metadata.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    current_time = datetime.now(tz).strftime("%A, %B %-d %Y, %I:%M %p %Z")
    parts.append(f"\nCurrent date and time: {current_time}")

    return "\n".join(parts)


_PY_TO_JSON: dict = {int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def _py_type_to_json(annotation: Any) -> str:
    import inspect as _inspect
    if annotation is _inspect.Parameter.empty:
        return "string"
    origin = getattr(annotation, "__origin__", None)
    if origin is list:
        return "array"
    if origin is dict:
        return "object"
    return _PY_TO_JSON.get(annotation, "string")


def _build_tool_schemas(
    tool_names: list[str],
    connector_tokens_db: dict[str, dict] | None = None,
    tools_config_map: dict[str, str] | None = None,
) -> list[dict]:
    import inspect
    registry = get_registry()
    schemas = []
    tool_names = list(dict.fromkeys(tool_names))
    for name in tool_names:
        connector_slug = (tools_config_map or {}).get(name, "")
        if connector_slug.startswith("mcp:"):
            mcp_ctx = (connector_tokens_db or {}).get(connector_slug, {})
            tool_def = mcp_ctx.get("tools", {}).get(name)
            if not tool_def:
                continue
            schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool_def.get("description", ""),
                    "parameters": tool_def.get("inputSchema", {"type": "object", "properties": {}}),
                },
            })
            continue
        fn = registry.get_callable(name)
        if not fn:
            continue
        sig = inspect.signature(fn)
        props = {}
        required = []
        for param_name, param in sig.parameters.items():
            if param_name in ("access_token", "instance_url", "kb_ids", "user_id", "collection_ids", "db_type"):
                continue
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
            props[param_name] = {
                "type": _py_type_to_json(param.annotation),
                "description": f"{param_name} parameter",
            }
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": getattr(fn, "tool_description", ""),
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        })
    return schemas


def _extract_sources(tool_results: list[dict]) -> list[Source]:
    sources: list[Source] = []
    seen: set[str] = set()
    for tr in tool_results:
        for s in tr.get("result", {}).get("sources", []):
            key = s.get("url") or s.get("title", "")
            if key and key not in seen:
                seen.add(key)
                sources.append(Source(**s))
    return sources


async def run_dynamic_agent(
    agent_input: AgentInput,
    agent_def: dict,
    model_id: str,
    api_key: str,
    connector_tokens_db: dict[str, dict] | None = None,
    is_embed: bool = False,
) -> AgentOutput:
    if agent_input.hitl_context and not agent_input.hitl_context.approved:
        return AgentOutput(
            agent_id=agent_input.agent_id,
            agent_name=agent_input.agent_name,
            task_description=agent_input.task,
            task_done=False,
            error="HITL denied by user",
        )

    tools_config_map: dict[str, str] = {
        tc["tool"]: tc.get("connector_slug", "")
        for tc in (agent_def.get("tools_config") or [])
        if isinstance(tc, dict) and tc.get("tool")
    }

    ctokens = connector_tokens_db or {}
    tool_schemas = _build_tool_schemas(agent_input.tools, ctokens, tools_config_map) if agent_input.tools else []
    system_prompt = _build_system_prompt(agent_def, agent_input, is_embed=is_embed)

    messages = [
        {"role": "system", "content": system_prompt},
        *agent_input.conversation_history[-settings.SHORT_TERM_MEMORY_WINDOW:],
        {"role": "user", "content": agent_input.task},
    ]

    result_text, tool_results, usage = await _run_with_tools(
        messages, tool_schemas, model_id, api_key, agent_input, ctokens, tools_config_map
    )

    return AgentOutput(
        agent_id=agent_input.agent_id,
        agent_name=agent_input.agent_name,
        task_description=agent_input.task,
        task_done=True,
        data={"response": result_text, "tool_results": tool_results},
        resource_usage={
            "tokens_used": usage.total_tokens,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "time_taken_ms": 0,
        },
        metadata={"model_used": model_id},
        sources=_extract_sources(tool_results),
    )


@retry(
    stop=stop_after_attempt(settings.LLM_MAX_RETRIES),
    wait=wait_exponential_jitter(
        initial=settings.LLM_RETRY_WAIT_MIN,
        max=settings.LLM_RETRY_WAIT_MAX,
        jitter=settings.LLM_RETRY_JITTER,
    ),
    retry=retry_if_exception(_is_retriable),
    before_sleep=before_sleep_log(logger, 30),
    reraise=True,
)
async def _run_with_tools(
    messages: list[dict],
    tool_schemas: list[dict],
    model_id: str,
    api_key: str,
    agent_input: AgentInput,
    connector_tokens_db: dict[str, dict],
    tools_config_map: dict[str, str] | None = None,
) -> tuple[str, list[dict], TokenUsage]:
    msgs = list(messages)
    all_tool_results: list[dict] = []
    total_input = total_output = 0
    total_cost = 0.0

    for _turn in range(5):
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": msgs,
            "api_key": api_key,
            "metadata": {"trace_name": f"dynamic_agent_{agent_input.agent_name}"},
        }
        if tool_schemas:
            kwargs["tools"] = tool_schemas
            kwargs["tool_choice"] = "auto"

        response = await litellm.acompletion(**kwargs)
        msg = response.choices[0].message
        u = calculate_usage(response, model_id)
        total_input += u.input_tokens
        total_output += u.output_tokens
        total_cost += u.cost_usd

        if not getattr(msg, "tool_calls", None):
            return msg.content or "", all_tool_results, TokenUsage(
                model=model_id,
                input_tokens=total_input,
                output_tokens=total_output,
                total_tokens=total_input + total_output,
                cost_usd=total_cost,
            )

        turn_results = await _execute_tool_calls(msg.tool_calls, connector_tokens_db, tools_config_map)
        all_tool_results.extend(turn_results)

        msgs.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tc, tr in zip(msg.tool_calls, turn_results):
            result_payload = tr.get("result") or {"error": tr.get("error", "Tool failed")}
            msgs.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": tc.function.name,
                "content": json.dumps(result_payload)[:8000],
            })

    # Exhausted turns — final call without tools
    response = await litellm.acompletion(
        model=model_id,
        messages=msgs,
        api_key=api_key,
        metadata={"trace_name": f"dynamic_agent_{agent_input.agent_name}"},
    )
    msg = response.choices[0].message
    u = calculate_usage(response, model_id)
    return msg.content or "", all_tool_results, TokenUsage(
        model=model_id,
        input_tokens=total_input + u.input_tokens,
        output_tokens=total_output + u.output_tokens,
        total_tokens=total_input + u.input_tokens + total_output + u.output_tokens,
        cost_usd=total_cost + u.cost_usd,
    )


async def _execute_tool_calls(
    tool_calls: list,
    connector_tokens_db: dict[str, dict],
    tools_config_map: dict[str, str] | None = None,
) -> list[dict]:
    registry = get_registry()
    results = []
    for tc in tool_calls:
        fn_name = tc.function.name
        connector_slug = (tools_config_map or {}).get(fn_name, "")
        if connector_slug.startswith("mcp:"):
            mcp_ctx = connector_tokens_db.get(connector_slug, {})
            try:
                args = json.loads(tc.function.arguments)
                result = await _call_mcp_tool(mcp_ctx, fn_name, args)
                results.append({"tool": fn_name, "result": result})
            except Exception as e:
                logger.exception("MCP tool %s execution failed", fn_name)
                results.append({"tool": fn_name, "error": f"MCP server unreachable or error: {e}"})
            continue
        fn = registry.get_callable(fn_name)
        if not fn:
            results.append({"tool": fn_name, "error": "Tool not found"})
            continue
        try:
            args = json.loads(tc.function.arguments)
            slug = getattr(fn, "connector", "")
            if slug == "__kb__":
                kb_tokens = connector_tokens_db.get("__kb__", {})
                args["kb_ids"] = kb_tokens.get("kb_ids", [])
                args["user_id"] = kb_tokens.get("user_id", "")
            elif slug == "__website__":
                wc_tokens = connector_tokens_db.get("__website__", {})
                args["collection_ids"] = wc_tokens.get("collection_ids", [])
                args["user_id"] = wc_tokens.get("user_id", "")
            elif slug and slug in connector_tokens_db:
                tokens = connector_tokens_db[slug]
                args["access_token"] = tokens.get("access_token", "")
                if "instance_url" in tokens:
                    args["instance_url"] = tokens["instance_url"]
                if "db_type" in tokens:
                    args["db_type"] = tokens["db_type"]
            result = await fn(**args)
            results.append({"tool": fn_name, "result": result})
        except (TypeError, json.JSONDecodeError) as e:
            logger.error("Tool %s argument error: %s", fn_name, e)
            results.append({"tool": fn_name, "error": str(e)})
        except Exception as e:
            logger.exception("Tool %s execution failed", fn_name)
            results.append({"tool": fn_name, "error": str(e)})
    return results


def _is_mcp_retriable(exc: Exception) -> bool:
    try:
        import httpx
        return isinstance(exc, (httpx.ConnectError, httpx.TimeoutException))
    except ImportError:
        return False


async def _call_mcp_tool(mcp_ctx: dict, tool_name: str, arguments: dict) -> dict:
    if mcp_ctx.get("transport_type") == "stdio":
        return await _call_mcp_tool_stdio(mcp_ctx, tool_name, arguments)
    return await _call_mcp_tool_http(
        mcp_ctx.get("server_url", ""),
        mcp_ctx.get("auth_type", "none"),
        mcp_ctx.get("access_token", ""),
        mcp_ctx.get("auth_header_name"),
        tool_name,
        arguments,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=4),
    retry=retry_if_exception(_is_mcp_retriable),
    reraise=True,
)
async def _call_mcp_tool_http(
    server_url: str,
    auth_type: str,
    token: str,
    auth_header_name: str | None,
    tool_name: str,
    arguments: dict,
) -> dict:
    import httpx
    headers = {"Content-Type": "application/json"}
    if token:
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "api_key" and auth_header_name:
            headers[auth_header_name] = token
    body = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}, "id": 1}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(server_url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")
    result = data.get("result", {})
    content = result.get("content", [])
    text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
    return {"text": text, "raw": result}


async def _call_mcp_tool_stdio(mcp_ctx: dict, tool_name: str, arguments: dict) -> dict:
    import asyncio
    import shlex
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parts = shlex.split(mcp_ctx.get("command") or "")
    if not parts:
        raise ValueError("stdio MCP server has no command")
    env = mcp_ctx.get("env_vars") or {}
    params = StdioServerParameters(command=parts[0], args=parts[1:], env=env or None)

    async def _run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool_name, arguments)

    try:
        result = await asyncio.wait_for(_run(), timeout=settings.MCP_TOOL_TIMEOUT_SECONDS)
    except BaseExceptionGroup as eg:
        from app.common.exceptions import MCPSubprocessError
        raise MCPSubprocessError(str(eg.exceptions[0])) from eg
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    return {"text": text, "isError": result.isError}
