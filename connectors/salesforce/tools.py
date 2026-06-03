"""Salesforce tool functions."""
from app.common.retry import async_http_request_with_retry
from tools.registry import tool


@tool(description="Search Salesforce records using SOQL", connector="salesforce")
async def salesforce_query(access_token: str, instance_url: str, soql: str) -> dict:
    """Run a SOQL query against Salesforce. Returns matching records."""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "GET",
            f"{instance_url}/services/data/v59.0/query",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": soql},
        )
    data = resp.json()
    return {"records": data.get("records", []), "total_size": data.get("totalSize", 0)}


@tool(description="Get a specific Salesforce record by ID", connector="salesforce")
async def salesforce_get_record(access_token: str, instance_url: str, sobject: str, record_id: str) -> dict:
    """Fetch a single Salesforce record by its ID and object type."""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "GET",
            f"{instance_url}/services/data/v59.0/sobjects/{sobject}/{record_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    return resp.json()


@tool(description="Create a Salesforce record", requires_hitl=True, connector="salesforce")
async def salesforce_create_record(
    access_token: str,
    instance_url: str,
    sobject: str,
    fields: dict,
) -> dict:
    """Create a new Salesforce record. Requires HITL approval."""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "POST",
            f"{instance_url}/services/data/v59.0/sobjects/{sobject}",
            headers={"Authorization": f"Bearer {access_token}"},
            json=fields,
        )
    data = resp.json()
    return {"id": data.get("id"), "success": data.get("success", False)}


@tool(description="Update a Salesforce record", requires_hitl=True, connector="salesforce")
async def salesforce_update_record(
    access_token: str,
    instance_url: str,
    sobject: str,
    record_id: str,
    fields: dict,
) -> dict:
    """Update fields on a Salesforce record. Requires HITL approval."""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "PATCH",
            f"{instance_url}/services/data/v59.0/sobjects/{sobject}/{record_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            json=fields,
        )
    return {"updated": True, "record_id": record_id}
