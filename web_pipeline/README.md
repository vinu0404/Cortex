# Web Pipeline — How It Works

The web pipeline lets users point an AI agent at a website. The site gets crawled, chunked, embedded, and stored in a vector database. When a user asks the agent a question, it searches those embeddings and injects relevant page snippets into the LLM context.

---

## Architecture Overview

```
User (browser)
    │
    │  POST /website-collections/{id}/urls/{url_id}/scrape
    ▼
FastAPI (app)
    │  sets status → pending
    │  enqueues Celery task
    │
    ├──────────────────────────────────────────────┐
    │                                              │
    │  GET /website-collections/status/stream      │
    │  (SSE — user watches live status)            │
    ▼                                              ▼
Redis PubSub                              Celery Worker
channel: wc_status:{user_id}                 │
    ▲                                            │  _run_crawl_pipeline()
    │  publishes on every status change          │
    │                                            │  1. load URL + API key from DB
    └────────────────────────────────────────────┤
                                                 │  2. set status → crawling
                                                 │
                                                 │  asyncio.create_subprocess_exec
                                                 │      python -m web_pipeline.runner
                                                 ▼
                                          OS Subprocess
                                          (web_pipeline/runner.py)
                                                 │
                                                 │  Scrapy CortexSpider
                                                 │  crawls pages, writes JSONL
                                                 ▼
                                          /tmp/xxxxxxxx.jsonl
                                                 │
                                                 ▼ (subprocess exits)
                                          Celery Worker (resumes)
                                                 │
                                                 │  3. read JSONL
                                                 │  4. set status → processing
                                                 │  5. delete old Qdrant chunks
                                                 │  6. chunk each page's text
                                                 │  7. embed chunks via user API key
                                                 │  8. upsert to Qdrant
                                                 │  9. set status → ready
                                                 ▼
                                          Qdrant collection: wc_{collection_id}
                                                 │
                              ┌──────────────────┘
                              │
                              │  At query time: agent asks a question
                              ▼
                        web_pipeline/retriever.py
                              │
                              │  cosine similarity search
                              │  returns top-k chunks with URL + title
                              ▼
                        injected into LLM system prompt as context
```

---

## Step-by-Step Flow

### Step 1 — User creates a collection

A **WebsiteCollection** is a named group of URLs. Think of it as a folder.

```
POST /website-collections
{
  "name": "FastAPI Docs",
  "description": "Official FastAPI documentation"
}
```

DB record created in `website_collections` table. No crawling yet.

---

### Step 2 — User adds a URL

```
POST /website-collections/{collection_id}/urls
{
  "url": "https://fastapi.tiangolo.com",
  "max_depth": 2
}
```

`max_depth` controls how many link levels to follow:
- `0` = only the start URL itself
- `1` = start URL + all pages it links to
- `2` = one more level deep from those pages

DB record in `website_urls` with status `pending`.

---

### Step 3 — User triggers scrape

```
POST /website-collections/{collection_id}/urls/{url_id}/scrape
```

FastAPI calls `WebsiteCollectionManager.trigger_scrape()`, which:
1. Sets `crawl_status = pending` in DB
2. Enqueues `crawl_website_task` on Celery with the `url_id`
3. Returns immediately — crawling is async

---

### Step 4 — Frontend connects to SSE stream

```
GET /website-collections/status/stream?token=<jwt>
```

Response is a persistent `text/event-stream`. The browser stays connected and receives events as the crawl progresses:

```
data: connected

: keepalive
: keepalive

data: {"url_id": "abc...", "status": "crawling", "url": "https://fastapi.tiangolo.com"}

: keepalive
: keepalive
...

data: {"url_id": "abc...", "status": "processing", "page_count": 47, "login_blocked_count": 0}

data: {"url_id": "abc...", "status": "ready", "page_count": 47, "chunk_count": 312}
```

Keepalives fire every ~4 seconds while waiting. They keep the connection alive through proxies/load balancers that would otherwise close idle streams.

---

### Step 5 — Celery task runs

`crawl_website_task` in [tasks.py](tasks.py) runs `_run_crawl_pipeline()`:

**a. Load from DB**
```python
wu = await db.scalar(select(WebsiteUrl).where(WebsiteUrl.id == UUID(url_id)))
```
Fetches the URL row. If cancelled or missing, exits cleanly.

**b. Fetch user's API key**
```python
key_result = await db.scalar(select(UserApiKey).where(UserApiKey.user_id == wu.user_id))
api_key = decrypt_str(key_result.encrypted_key)
```
Used later to call the embedding model. Each user's content is embedded with their own key.

**c. Status → crawling**
```python
wu.crawl_status = WcCrawlStatusEnum.crawling
await db.commit()
await _publish_wc_status(redis_client, user_id, ..., "crawling", url)
```
DB updated, Redis event published → browser SSE receives `crawling` event.

---

### Step 6 — Spider subprocess

This is the core of the pipeline.

**Why a subprocess?**

Scrapy uses the Twisted event loop reactor internally. The reactor:
- Can only be started **once per process** — restarting it after it stops raises `ReactorNotRestartable`
- Cannot run inside an already-running event loop (asyncio conflict)

Celery workers are daemonic processes, which means Python's `multiprocessing.Process` is forbidden (`AssertionError: daemonic processes are not allowed to have children`).

Solution: OS-level subprocess via `asyncio.create_subprocess_exec`. This bypasses both restrictions — each crawl gets its own fresh process with its own reactor.

```python
proc = await asyncio.create_subprocess_exec(
    sys.executable, "-m", "web_pipeline.runner",
    url, str(max_depth), output_path, str(max_pages), json.dumps(cfg),
    stdout=asyncio.subprocess.DEVNULL,
    stderr=asyncio.subprocess.PIPE,
)
_, stderr_bytes = await asyncio.wait_for(
    proc.communicate(), timeout=WC_CRAWL_TIMEOUT_SECONDS
)
```

- `stdout` is discarded (spider writes output to a temp file, not stdout)
- `stderr` is captured — if the spider crashes, the error message appears in the Celery logs
- `asyncio.wait_for` applies the timeout asynchronously — the Celery worker is not blocked; it can process other tasks
- On timeout: `proc.kill()` (SIGKILL, not SIGTERM — guaranteed termination)

**What runner.py does**

[runner.py](runner.py) is a minimal standalone script:
1. Parses the 5 CLI args
2. Creates a `CrawlerProcess` with the settings passed from the parent task
3. Starts `CortexSpider`
4. Exits with code 0 on success, code 2 on spider error

If it exits with non-zero, the parent task reads stderr and raises `RuntimeError` with the error message, which triggers a Celery retry.

---

### Step 7 — CortexSpider crawls

[spider.py](spider.py) is a Scrapy `CrawlSpider`. It:

1. **Starts at `start_url`**, e.g. `https://fastapi.tiangolo.com`
2. **Follows all links** on the same domain (`allowed_domains = [urlparse(start_url).netloc]`)
   - `deny_extensions=[]` means it follows all link types (including `.html`, `.php`, etc.)
   - Links to other domains are ignored
3. **Detects login walls** via `_is_login_wall()`:
   - URL contains `/login`, `/signin`, `/auth`, etc. → blocked
   - HTTP status 401 or 403 → blocked
   - Page body has `<input type="password">` → blocked
   - Page text contains "sign in to", "login required", etc. → blocked
4. **Extracts page text**:
   ```python
   response.css("script, style, nav, header, footer").drop()
   text = " ".join(response.css("body *::text").getall())
   ```
   Strips navigation chrome and JS/CSS noise, keeps body content.
5. **Writes JSONL** to the temp file, one JSON object per page:
   ```json
   {"url": "https://fastapi.tiangolo.com/tutorial/", "title": "Tutorial", "text": "...", "depth": 1}
   ```
   Login-blocked pages are written with `{"login_blocked": true, "url": "...", "is_start_url": false}`.

**Page cap**: stops at `WC_MAX_PAGES_PER_URL` pages total.
**Text cap**: each page's text is truncated at 50,000 characters.

---

### Step 8 — Back in Celery: read + validate output

```python
with open(output_path, "r") as f:
    items = [json.loads(line) for line in f if line.strip()]

pages = [item for item in items if not item.get("login_blocked")]
blocked = [item for item in items if item.get("login_blocked")]
```

**Error cases**:
- Start URL itself is login-blocked → `ValueError("login_required: ...")` — non-retriable, user must fix the URL
- All pages login-blocked → same error
- No pages at all → `RuntimeError` → retried up to 2 times

---

### Step 9 — Chunk

Each page's text is split into overlapping chunks via the shared `chunk_document()` pipeline:

```
"FastAPI is a modern web framework for building APIs with Python..."
        ↓
Chunk 0: "FastAPI is a modern web framework for building APIs..."
Chunk 1: "...for building APIs with Python. It is based on standard..."
Chunk 2: "...based on standard Python type hints. The key features..."
```

Chunks keep overlap so a sentence that crosses a chunk boundary is still findable.

---

### Step 10 — Embed

```python
embeddings = await embed_texts([c.text for c in all_chunks], api_key)
```

Calls the user's configured embedding model (same model used for knowledge base documents). Each chunk text becomes a vector of ~1536 or 3072 floats depending on the model.

---

### Step 11 — Upsert to Qdrant

```python
await wc_vs.ensure_collection(collection_id, redis_client)   # creates if not exists
await wc_vs.upsert_chunks(collection_id, url_id, chunks, embeddings, payloads)
```

Qdrant collection name: `wc_{collection_id}`

Each point stored in Qdrant:
```json
{
  "id": "uuid",
  "vector": [0.012, -0.847, ...],
  "payload": {
    "url_id": "3041a2f8-...",
    "collection_id": "0da554a2-...",
    "text": "FastAPI is a modern web framework...",
    "url": "https://fastapi.tiangolo.com/tutorial/",
    "title": "Tutorial - FastAPI",
    "depth": 1,
    "chunk_index": 0
  }
}
```

Old chunks for the same `url_id` are deleted before upserting (idempotent re-crawl — re-scraping the same URL replaces its content cleanly).

---

### Step 12 — Status → ready

```python
wu.crawl_status = WcCrawlStatusEnum.ready
wu.page_count = 47
wu.chunk_count = 312
wu.last_crawled_at = datetime.now(timezone.utc)
await db.commit()
await _publish_wc_status(..., "ready", ...)
```

Browser receives the `ready` SSE event. The URL row now shows page and chunk counts.

---

### Step 13 — Agent uses the collection at query time

When a user sends a message to an agent that has this collection attached:

```python
# web_pipeline/retriever.py
results = await vector_store.dense_search(collection_id, query_embedding, top_k=5)
```

The query text is embedded using the same model, then Qdrant does a cosine similarity search. The top-k most relevant chunks (by semantic similarity) are returned with their source URL and title, then injected into the LLM's system prompt as grounding context.

For multiple collections attached to one agent:
```python
results = await retrieve_multi([cid1, cid2, cid3], query_embedding, top_k=5)
```
Results from all collections are merged and re-ranked by score before injection.

---

## Status State Machine

```
           trigger_scrape()
pending ──────────────────► crawling
                                │
                    spider OK   │   spider timeout/crash
                    ┌───────────┤────────────────────────► failed
                    │           │                          (retried up to 2×)
                    ▼           │
               processing       │ login_required (ValueError)
                    │           └────────────────────────► failed (no retry)
          embed+upsert OK
                    │
                    ▼
                  ready  ◄──── re-scrape (replaces chunks)
                    │
              cancel_url()
                    ▼
               cancelled
```

- **`failed`** — error message stored in `website_urls.error_message` (truncated to 2000 chars)
- **`cancelled`** — Celery task may still be running but will exit cleanly when it checks the status
- **`ready`** — normal operating state; agent can query this URL's chunks

---

## Concrete Example: Crawling the FastAPI Docs

**1. Create collection**
```bash
curl -X POST /website-collections \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "FastAPI Docs"}'
# → {"id": "0da554a2-...", "name": "FastAPI Docs"}
```

**2. Add URL with depth 2**
```bash
curl -X POST /website-collections/0da554a2-.../urls \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"url": "https://fastapi.tiangolo.com", "max_depth": 2}'
# → {"id": "3041a2f8-...", "crawl_status": "pending"}
```

**3. Open SSE stream in browser** (JavaScript)
```javascript
const es = new EventSource(`/website-collections/status/stream?token=${jwt}`);
es.onmessage = (e) => {
  const data = JSON.parse(e.data);
  console.log(data.status, data.page_count);
};
```

**4. Trigger scrape**
```bash
curl -X POST /website-collections/0da554a2-.../urls/3041a2f8-.../scrape \
  -H "Authorization: Bearer $TOKEN"
```

**5. Watch events arrive over SSE**
```
connected
crawling  {"url": "https://fastapi.tiangolo.com", "status": "crawling"}
processing {"status": "processing", "page_count": 47}
ready      {"status": "ready", "page_count": 47, "chunk_count": 312}
```

**6. Ask the agent a question**

User: "How do I define path parameters in FastAPI?"

Internally:
- Query embedded → cosine search in `wc_0da554a2-...`
- Top result: chunk from `https://fastapi.tiangolo.com/tutorial/path-params/`, score 0.94
- Injected into LLM context: `[FastAPI Docs — Path Parameters] You can declare path parameters...`
- LLM answers using real documentation content

---

## Error Example: Login-Walled Site

```bash
curl -X POST .../urls -d '{"url": "https://app.example.com/dashboard", "max_depth": 1}'
curl -X POST .../urls/xxx/scrape
```

SSE events:
```
crawling  {"url": "https://app.example.com/dashboard", "status": "crawling"}
failed    {"status": "failed", "error_message": "login_required: Website requires login..."}
```

`crawl_status` in DB → `failed`. No retry (ValueError is non-retriable). User needs to provide a public URL.

---

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `WC_CRAWL_TIMEOUT_SECONDS` | `600` | Max seconds before spider subprocess is killed |
| `WC_MAX_PAGES_PER_URL` | `100` | Page cap per URL |
| `WC_OBEY_ROBOTS` | `True` | Whether to respect robots.txt |
| `WC_USER_AGENT` | `CortexBot/1.0` | HTTP User-Agent sent by spider |
| `WC_CONCURRENT_REQUESTS` | `4` | Scrapy parallel request count |
| `WC_DOWNLOAD_TIMEOUT` | `30` | Per-request timeout in seconds |

---

## Files

| File | Purpose |
|---|---|
| [tasks.py](tasks.py) | Celery task — orchestrates the full pipeline |
| [runner.py](runner.py) | Standalone subprocess entry point for Scrapy |
| [spider.py](spider.py) | Scrapy CrawlSpider — link extraction + page parsing |
| [vector_store.py](vector_store.py) | Qdrant read/write operations |
| [retriever.py](retriever.py) | Query-time cosine search, multi-collection merge |
| [../app/website_collections/](../app/website_collections/) | HTTP API, DB models, collection manager |
