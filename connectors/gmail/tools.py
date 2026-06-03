"""Gmail tool functions. Each is auto-discovered by ToolRegistry."""
from app.common.retry import async_http_request_with_retry
from tools.registry import tool


@tool(description="Read emails from Gmail inbox", connector="gmail")
async def gmail_read_mail(access_token: str, max_results: int = 10, query: str = "") -> dict:
    """Fetch emails from Gmail. Returns list of email summaries."""
    import httpx
    params = {"maxResults": max_results}
    if query:
        params["q"] = query
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "GET",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        messages = resp.json().get("messages", [])

    emails = []
    async with httpx.AsyncClient() as client:
        for msg in messages[:max_results]:
            detail = await async_http_request_with_retry(
                client,
                "GET",
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
            )
            headers = {h["name"]: h["value"] for h in detail.json().get("payload", {}).get("headers", [])}
            emails.append({
                "id": msg["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": detail.json().get("snippet", ""),
            })
    return {"emails": emails, "count": len(emails)}


@tool(description="Send an email via Gmail", requires_hitl=True, connector="gmail")
async def gmail_send_mail(
    access_token: str,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
) -> dict:
    """Send an email through the user's Gmail account. Requires HITL approval."""
    import base64
    import email as email_lib
    from email.mime.text import MIMEText

    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    if cc:
        msg["cc"] = cc

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    import httpx
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"raw": raw},
        )
    return {"sent": True, "message_id": resp.json().get("id")}


@tool(description="Create a draft email in Gmail", connector="gmail")
async def gmail_create_draft(
    access_token: str,
    to: str,
    subject: str,
    body: str,
) -> dict:
    """Create a draft email in Gmail without sending."""
    import base64
    from email.mime.text import MIMEText

    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    import httpx
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"message": {"raw": raw}},
        )
    return {"draft_id": resp.json().get("id"), "created": True}


@tool(description="List Gmail labels", connector="gmail")
async def gmail_list_labels(access_token: str) -> dict:
    """Fetch all Gmail labels for the user."""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await async_http_request_with_retry(
            client,
            "GET",
            "https://gmail.googleapis.com/gmail/v1/users/me/labels",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    labels = [{"id": l["id"], "name": l["name"]} for l in resp.json().get("labels", [])]
    return {"labels": labels}
