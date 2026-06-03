"""Google Calendar tool functions."""
from app.common.retry import async_http_request_with_retry
from tools.registry import tool


@tool(description="List upcoming calendar events", connector="calendar")
async def calendar_list_events(access_token: str, max_results: int = 10, calendar_id: str = "primary") -> dict:
    """List upcoming events from Google Calendar."""
    from datetime import datetime, timezone
    import httpx

    now = datetime.now(timezone.utc).isoformat()
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "GET",
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"timeMin": now, "maxResults": max_results, "singleEvents": True, "orderBy": "startTime"},
        )
    events = []
    for e in resp.json().get("items", []):
        start = e.get("start", {})
        events.append({
            "id": e["id"],
            "summary": e.get("summary", ""),
            "start": start.get("dateTime") or start.get("date", ""),
            "location": e.get("location", ""),
            "description": e.get("description", ""),
        })
    return {"events": events}


@tool(description="Create a calendar event", requires_hitl=True, connector="calendar")
async def calendar_create_event(
    access_token: str,
    summary: str,
    start_datetime: str,
    end_datetime: str,
    description: str = "",
    location: str = "",
    attendees: list[str] | None = None,
    calendar_id: str = "primary",
) -> dict:
    """Create a new event in Google Calendar. Requires HITL approval. start/end in ISO 8601."""
    import httpx

    event: dict = {
        "summary": summary,
        "description": description,
        "location": location,
        "start": {"dateTime": start_datetime, "timeZone": "UTC"},
        "end": {"dateTime": end_datetime, "timeZone": "UTC"},
    }
    if attendees:
        event["attendees"] = [{"email": a} for a in attendees]

    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "POST",
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
            headers={"Authorization": f"Bearer {access_token}"},
            json=event,
        )
    data = resp.json()
    return {"event_id": data["id"], "html_link": data.get("htmlLink", ""), "created": True}


@tool(description="Delete a calendar event", requires_hitl=True, connector="calendar")
async def calendar_delete_event(access_token: str, event_id: str, calendar_id: str = "primary") -> dict:
    """Delete a calendar event by ID. Requires HITL approval."""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "DELETE",
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    return {"deleted": True, "event_id": event_id}
