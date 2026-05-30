# Cortex — Multi-Agent Orchestration Platform

A workspace-based multi-agent AI platform built with FastAPI and LiteLLM. Users build workspaces containing custom agents, connect them to real-world tools (Gmail, GitHub, Calendar, Salesforce, Web Search), attach **Knowledge Bases** (file uploads → Qdrant) and **Website Collections** (web crawl → Qdrant), then chat with the assembled workspace. Agents are orchestrated via **Kahn's topological sort** — independent agents run in parallel, dependent agents run in sequence. No LangChain, no LangGraph.

**Key design:** Each workspace has a non-editable **Master Agent** that plans execution (which agents, in what order, with what tools) and a non-editable **Composer Agent** that synthesizes the final streamed answer. Users define custom agents in between. File ingestion and web crawling run as **Celery background tasks** — results streamed to the frontend via Redis Pub/Sub → SSE.

---

## How to Run

### Option A — Full Docker (recommended)

Everything runs in containers. No local Python needed.

```bash
# 1. Copy and fill env
cp .env.example .env
# Edit .env — at minimum: JWT_SECRET, ENCRYPTION_KEY, LANGFUSE_*

# 2. Build images + start all 5 services + migrate + seed (first run)
make setup

# Or step by step:
make build    # build images and start containers
make migrate  # apply Alembic migrations
make seed     # seed Langfuse prompts
```

App available at `http://localhost:8000`.

### Makefile reference

| Target | Command | What it does |
|--------|---------|--------------|
| `make setup` | `build migrate seed` | Full first-run: build + migrate + seed |
| `make build` | `docker compose up -d --build` | Build images and start all containers |
| `make up` | `docker compose up -d` | Start containers (no rebuild) |
| `make down` | `docker compose down` | Stop containers |
| `make restart` | `down + build` | Full restart with rebuild |
| `make migrate` | `alembic upgrade head` (in app container) | Apply pending migrations |
| `make seed` | `python seed_langfuse.py` (in app container) | Seed Langfuse prompts |
| `make logs` | `docker compose logs -f app` | Tail FastAPI app logs |
| `make logs-worker` | `docker compose logs -f celery_worker` | Tail Celery worker logs |
| `make logs-all` | `docker compose logs -f` | Tail all service logs |
| `make shell` | `docker exec -it cortex_app-app-1 bash` | Open shell in app container |
| `make ps` | `docker compose ps` | Show container status |
| `make clean` | `docker compose down -v` | Stop containers and delete volumes |
| `make nuke` | `docker compose down -v --rmi all` | Full wipe: containers + volumes + images |

| Service | Container | Port | Memory limit |
|---------|-----------|------|-------------|
| FastAPI app | `cortex_app-app-1` | `8000` | 1 GB |
| Celery worker | `cortex_app-celery_worker-1` | — | 512 MB |
| PostgreSQL 16 | `cortex_app-postgres-1` | `5432` | 512 MB |
| Redis 7 | `cortex_app-redis-1` | `6379` | 256 MB |
| Qdrant | `cortex_app-qdrant-1` | `6333` | 512 MB |

All services have health checks with retry logic. The app and Celery worker wait for Postgres, Redis, and Qdrant to be healthy before starting.

---

### Option B — Local

#### Prerequisites

- **Python 3.11+**
- **Docker** (for PostgreSQL, Redis, Qdrant)
- **Scrapy** — for website crawling (`pip install scrapy`)
- A **Langfuse** account (cloud or self-hosted) — all prompts live there; app fails to start without it
- At least one LLM API key (OpenAI, Anthropic, Gemini, or Groq) — used for embeddings + LLM calls

#### 1. Start Infrastructure

```bash
docker compose up -d postgres redis qdrant
```

| Service | Port | Purpose |
|---------|------|---------|
| PostgreSQL 16 | `5432` | All relational data — users, workspaces, agents, messages, KB, WC |
| Redis 7 | `6379/0` | HITL pub/sub, OAuth CSRF state, token budget counters |
| Redis 7 | `6379/1` | Celery broker + backend |
| Qdrant | `6333` | Vector store — KB collections (`kb_{id}`) + WC collections (`wc_{id}`) |

#### 2. Create Virtual Environment

```bash
python -m venv .venv
source .venv/bin/activate          # Linux / macOS
.\.venv\Scripts\activate           # Windows

pip install -r requirements.txt
```

#### 3. Configure Environment

```bash
cp .env.example .env
```

At minimum: `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET`, `ENCRYPTION_KEY`, `LANGFUSE_*`.

#### 4. Seed Langfuse Prompts

```bash
python seed_langfuse.py
```

Required prompt names: `master_agent`, `composer_agent`, `memory_compression`, `long_term_memory_extraction`, `title_generation`, `suggestion_generation`, `agent_prompt_generator`

#### 5. Apply Database Migrations

```bash
alembic upgrade head
```

Creates all tables: `users`, `workspaces`, `agents`, `conversations`, `messages`, `knowledge_bases`, `kb_documents`, `agent_knowledge_bases`, `website_collections`, `website_urls`, `agent_website_collections`, `message_artifacts`, `personas`, `conversation_summaries`, `refresh_tokens`, `connector_instances`, `connector_definitions`, `hitl_requests`, `user_long_term_memory`.

#### 6. Start the Server

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

#### 7. Start Celery Worker

```bash
celery -A celery_app worker --loglevel=info --concurrency=4
```

---

| URL | What |
|-----|------|
| `http://localhost:8000` | Workspace home |
| `http://localhost:8000/dashboard.html` | User dashboard |
| `http://localhost:8000/admin.html` | Admin panel (admin role only) |
| `http://localhost:8000/knowledge-bases.html` | Knowledge Base manager |
| `http://localhost:8000/website-collections.html` | Website Collection manager |
| `http://localhost:8000/workspace.html?id=<uuid>` | Agent builder |
| `http://localhost:8000/chat.html?workspace_id=<uuid>` | Chat UI |
| `http://localhost:8000/docs` | Swagger (dev only) |

---

## System Architecture

```mermaid
graph TB
    User["User (Browser)"]

    User -->|REST + SSE| API["FastAPI Layer"]

    subgraph Startup["Startup (lifespan)"]
        Seed["Seed connector definitions"]
        Discover["ToolRegistry.auto_discover()"]
        LFHook["LiteLLM → Langfuse callback"]
    end

    API -->|POST /chat/stream| Stream["SSE Streaming Pipeline"]
    API -->|presigned PUT URL + POST confirm| KBCtrl["KB Controller"]
    API -->|POST website-collections scrape| WCCtrl["WC Controller"]

    Stream --> LoadCtx["Load workspace context\nagents + api keys + connector tokens\nkb_ids + collection_ids per agent"]
    LoadCtx --> Master["Master Agent\nLiteLLM + Langfuse prompt\nresponse_format=ExecutionPlan"]
    Master --> Orch["Orchestrator\nKahn toposort → stages"]

    subgraph Stage1["Stage 1 — asyncio.gather"]
        A1["Agent A\ntools + knowledge_base_search"]
        A2["Agent B\ntools + collection_search"]
    end

    Orch --> Stage1
    Stage1 --> Composer["Composer Agent\nstreams answer token by token"]
    Composer -->|SSE token events| User

    KBCtrl -->|enqueue| Celery["Celery Worker"]
    WCCtrl -->|enqueue| Celery

    subgraph DocPipeline["Document Pipeline"]
        Parse["Parser\nPDF/DOCX/CSV/images"]
        Chunk["Chunker\noverlapping text windows"]
        Embed["Embedder\nOpenAI text-embedding-3-small"]
        QdrantKB[("Qdrant\nkb_{id}")]
    end

    subgraph WebPipeline["Web Pipeline"]
        Spider["Scrapy CortexSpider\nmultiprocessing.Process\nlogin-wall detection"]
        WChunk["Chunker"]
        WEmbed["Embedder"]
        QdrantWC[("Qdrant\nwc_{id}")]
    end

    Celery --> DocPipeline
    Celery --> WebPipeline

    DocPipeline -.->|Redis PUBLISH kb_status| API
    WebPipeline -.->|Redis PUBLISH wc_status| API
    API -.->|SSE status stream| User

    subgraph Persistence
        PG[("PostgreSQL")]
        Redis[("Redis")]
    end

    API <--> PG
    Stream <--> Redis
    A1 -->|dense+sparse search| QdrantKB
    A2 -->|dense search| QdrantWC
```

---

## Workspace Model

Each user creates **workspaces**. A workspace is a collection of agents that collaborate to answer queries.

```mermaid
graph LR
    subgraph Workspace["Workspace — Sales Assistant"]
        MA["Master Agent\nplans execution\nnot editable"]
        A1["ResearchAgent\ntools: web_search\nKB: product_docs\nWC: competitor_site"]
        A2["EmailAgent\ntools: gmail_send_mail"]
        CA["Composer Agent\nsynthesizes answer\nnot editable"]
    end

    Query["User query"] --> MA
    MA -->|plan| A1
    MA -->|plan| A2
    A1 -->|results| CA
    A2 -->|results| CA
    CA -->|SSE stream| Answer["Streamed answer"]
```

- **Master** and **Composer** auto-created with every workspace (`is_editable=false`)
- Custom agents have: name, system prompt, LLM model + API key, tools, KB attachments, WC attachments
- Agents can be assigned **Knowledge Bases** (file-indexed data) and **Website Collections** (crawled web data)
- At chat time, `knowledge_base_search` and `collection_search` tools are **auto-injected** — agents don't need manual tool selection

---

## Knowledge Bases

Users upload files → Celery ingests → chunks embedded and stored in Qdrant → agents query at runtime.

### Ingestion Pipeline

```mermaid
flowchart TD
    Presign["POST /knowledge-bases/{id}/documents/presign\nfilename + content_type + file_size_bytes"] --> GenURL["Backend: create KbDocument\nstatus=pending_upload\ngenerate presigned PUT URL"]
    GenURL --> Direct["Browser PUT directly to B2\nno backend bandwidth or RAM"]
    Direct --> Confirm["POST /knowledge-bases/{id}/documents/{doc_id}/confirm\nbackend triggers Celery task"]
    Confirm --> Task["Celery: process_document_task\nacks_late + max_retries=2"]
    Task --> SetProc["DB: status=processing\nRedis PUBLISH kb_status:user_id"]

    SetProc --> Parse["Parser\nPDF→pypdf, DOCX→python-docx\nCSV→pandas, image→GPT-4o vision"]
    Parse --> Chunk["Chunker\nsize=1000 tokens, overlap=200\nCSV: 100 rows/chunk"]
    Chunk --> Embed["Embedder\ntext-embedding-3-small, dim=1536\nbatch=96"]

    Embed --> Qdrant["Qdrant collection: kb_{kb_id}\nDense vector + sparse BM25 text index\nPayload: doc_id, chunk_index, text, section"]
    Qdrant --> Ready["DB: status=ready, chunk_count\nRedis PUBLISH ready → SSE update"]

    Task -->|on_failure| Failed["DB: status=failed, error_message\nRedis PUBLISH failed → SSE update"]
```

### Retrieval at Chat Time

```mermaid
flowchart LR
    Query["Agent task"] --> Embed2["embed_texts(query)"]
    Embed2 --> Dense["Dense search\ntop_k=50, cosine similarity"]
    Embed2 --> Sparse["Sparse search\nBM25 text index, top_k=50"]
    Dense --> RRF["Reciprocal Rank Fusion\nk=60, merge + rerank"]
    Sparse --> RRF
    RRF --> Top["top_k=5 final chunks"]
    Top --> LLM["Injected into agent context"]
```

### Supported File Types

| Extension | Parser | Notes |
|-----------|--------|-------|
| `.pdf` | pypdf (text) + GPT-4o (images) | Text pages parsed directly; scanned/image PDFs use vision |
| `.docx` / `.doc` | python-docx | Paragraphs + tables |
| `.xlsx` / `.xls` | openpyxl | Each sheet chunked as CSV rows |
| `.csv` | pandas | `KB_CSV_ROWS_PER_CHUNK` rows per chunk |
| `.txt` / `.md` | plain text | UTF-8 |
| `.png` / `.jpg` / `.jpeg` / `.webp` / `.gif` / `.bmp` | GPT-4o vision | Returns text description |

---

## Website Collections

Users create collections → add URLs with crawl depth → trigger scrape → Scrapy crawls → text embedded → agents query at runtime.

### Crawl Pipeline

```mermaid
flowchart TD
    AddURL["POST /website-collections/{id}/urls\nurl + max_depth 1–5"] --> Pending["DB: crawl_status=pending"]
    Pending --> Trigger["POST .../scrape\nor Scrape All"]
    Trigger --> Task["Celery: crawl_website_task\nacks_late + max_retries=2"]
    Task --> SetCrawl["DB: status=crawling\nRedis PUBLISH wc_status:user_id"]

    SetCrawl --> Process["multiprocessing.Process\nCortexSpider — fresh Twisted reactor"]
    Process --> JSONL["JSONL temp file\nnormal pages + login_blocked sentinels"]

    JSONL --> LoginCheck{"Start URL\nlogin wall?"}
    LoginCheck -->|Yes| LoginFail["ValueError login_required:\nDB: status=failed\nerror_message starts with login_required:\nUI shows Remove only"]
    LoginCheck -->|No| Chunk2["Chunker + Embedder\ntext-embedding-3-small"]

    Chunk2 --> QdrantWC["Qdrant: wc_{collection_id}\ndense vectors only\nPayload: url, title, depth, chunk_index"]
    QdrantWC --> Ready2["DB: status=ready\npage_count + chunk_count + login_blocked_count\nRedis PUBLISH ready → SSE update"]

    Task -->|on_failure| Failed2["DB: status=failed, error_message\nRedis PUBLISH failed → SSE update"]
```

### Login-Wall Detection

The spider detects login pages and skips them (does not follow links from them):

| Signal | Check |
|--------|-------|
| URL pattern | `/login`, `/signin`, `/sign-in`, `/auth`, `/sso`, `/oauth`, `?redirect=`, `?next=` |
| HTTP status | `401` or `403` |
| HTML content | `<input type="password">` present in first 3000 chars |
| Text signals | "sign in to", "please log in", "login required", "access denied", "you must be logged in" |

**UI behavior:**
- `error_message.startsWith("login_required:")` → **Remove** button only (no Retry)
- All other failures → **Retry** + Delete buttons
- `login_blocked_count > 0` on ready URL → ⚠️ amber badge "N pages need login"

### URL Status States

```mermaid
stateDiagram-v2
    [*] --> pending: URL added
    pending --> crawling: scrape triggered
    crawling --> processing: spider finished, pages found
    processing --> ready: chunks embedded in Qdrant
    crawling --> failed: spider error / timeout
    processing --> failed: embed error
    ready --> crawling: re-scrape triggered
    failed --> pending: retry triggered
    failed --> [*]: Remove (login_required errors only)
```

---

## Agent Builder

`workspace.html` — configure agents with tools, KB, and website collection attachments.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  MASTER (locked)     [ResearchAgent]     [EmailAgent]     COMPOSER (locked)    │
│                                                                                 │
│  Add/Edit Agent Modal:                                                          │
│  ├── Name + System Prompt + Model                                               │
│  ├── Tools           [ ] web_search  [ ] gmail_send_mail 🔒  [ ] github_issues │
│  ├── Knowledge Bases [ ] product_docs  [ ] support_wiki                        │
│  └── Website Cols    [ ] competitor_site  [ ] docs_site                        │
├────────────────────────────────────────────────────────────────────────────────┤
│  Sidebar                                                                        │
│  ├── API Keys    [+ Add Key]  (auto-detects provider + models)                  │
│  └── Connectors  [Gmail ✓ Connected]  [GitHub ✓]  [Web Search Built-in]       │
└────────────────────────────────────────────────────────────────────────────────┘
```

Agent cards show badges: `🔧 tool_name`, `📚 kb_name` (purple), `🌐 wc_name` (green)

---

## Orchestration — Kahn's Topological Sort

`core/dependency_resolver.py` — Kahn's algorithm on the plan's `depends_on` edges.

### Pattern 1: Parallel (no dependencies)

```mermaid
graph LR
    Master --> Orch

    subgraph parallel["Stage 1 — asyncio.gather"]
        A["EmailAgent\ngmail_read_mail"]
        B["WebAgent\nweb_search"]
    end

    Orch --> A & B
    A & B --> Composer
    style A fill:#4CAF50,color:#fff
    style B fill:#2196F3,color:#fff
```

### Pattern 2: Sequential (linear chain)

```mermaid
graph LR
    Master --> Orch

    subgraph s1["Stage 1"]
        A["DataAgent\ngmail_read_mail"]
    end

    subgraph s2["Stage 2"]
        B["AnalystAgent\nuses A output"]
    end

    subgraph s3["Stage 3"]
        C["EmailAgent\ngmail_send_mail\nuses B output"]
    end

    Orch --> A --> B --> C --> Composer
    style A fill:#4CAF50,color:#fff
    style B fill:#FF9800,color:#fff
    style C fill:#9C27B0,color:#fff
```

### Pattern 3: Diamond (fan-out + fan-in)

```mermaid
graph TD
    Master --> Orch

    subgraph p1["Stage 1 — parallel"]
        A["DocAgent\nknowledge_base_search"]
        B["WebAgent\ncollection_search"]
    end

    subgraph p2["Stage 2 — waits for both"]
        C["AnalystAgent\nreceives A+B outputs"]
    end

    Orch --> A & B
    A & B --> C --> Composer
    style A fill:#4CAF50,color:#fff
    style B fill:#2196F3,color:#fff
    style C fill:#FF9800,color:#fff
```

### How Kahn's Algorithm Creates Stages

```mermaid
flowchart TD
    Input["PlanSteps with depends_on edges"] --> Build["Build in-degree map + adjacency graph"]
    Build --> Init["Queue all steps with in-degree = 0"]
    Init --> Loop{"Queue empty?"}
    Loop -->|No| Drain["Drain queue → one parallel Stage"]
    Drain --> Dec["Decrement in-degree of dependents"]
    Dec --> Enq["Enqueue newly zero-in-degree steps"]
    Enq --> Loop
    Loop -->|Yes| Check{"Remaining nodes?"}
    Check -->|"Yes — cycle!"| Err["CircularDependencyError → error SSE"]
    Check -->|No| Done["Return stages list"]

    style Err fill:#f44336,color:#fff
    style Done fill:#4CAF50,color:#fff
```

---

## SSE Streaming Events

`POST /chat/stream` — all real-time communication via Server-Sent Events.

```
event: plan          {"execution_order": "Master → ResearchAgent[knowledge_base_search] → Composer"}
event: status        {"phase": "planning|executing|composing", "agent_name": "ResearchAgent"}
event: hitl_required {"request_id": "...", "agent_name": "...", "tool_names": ["gmail_send_mail"], "timeout_seconds": 120}
event: hitl_approved {"request_id": "...", "instructions": "..."}
event: hitl_denied   {"request_id": "..."}
event: compacting    {"message": "Summarising earlier conversation..."}
event: token         {"text": "..."}
event: artifact      {"type": "code|table|chart", "title": "...", "content": "..."}
event: suggestions   {"questions": ["...", "...", "..."]}
event: done          {"total_ms": ..., "conversation_id": "...", "message_id": "..."}
event: error         {"message": "..."}
```

**KB/WC status SSE** (separate streams):
```
GET /knowledge-bases/status/stream?token=...
GET /website-collections/status/stream?token=...

data: {"kb_id|collection_id": "...", "url_id": "...", "status": "crawling|processing|ready|failed",
       "page_count": 12, "chunk_count": 48, "login_blocked_count": 2}
```

Frontend uses `EventSource` for KB/WC status streams. Chat uses `fetch` + `ReadableStream` (EventSource can't send JWT auth headers).

---

## Human-in-the-Loop (HITL)

Tools marked `requires_hitl=True` pause execution and ask the user to approve before the tool runs. Works **across multiple server workers** via Redis pub/sub.

```mermaid
sequenceDiagram
    participant SSE as SSE Stream
    participant Redis as Redis pub/sub
    participant User as User (Browser)
    participant HITL as POST /hitl/respond

    SSE->>SSE: Agent needs gmail_send_mail (requires_hitl=True)
    SSE->>Redis: Subscribe hitl:{request_id}
    SSE->>User: event: hitl_required

    alt User approves
        User->>HITL: {approved: true, instructions: "only to john@"}
        HITL->>Redis: PUBLISH hitl:{request_id} {approved: true}
        Redis->>SSE: Message received → resume
        SSE->>User: event: hitl_approved
    else User denies
        User->>HITL: {approved: false}
        SSE->>User: event: hitl_denied
        SSE->>SSE: AgentOutput.error = "HITL denied by user"
    else Timeout (120s)
        SSE->>SSE: Auto-deny, Composer notes missing output
    end
```

---

## Memory System

### Short-Term (per conversation)

Sliding window of `SHORT_TERM_MEMORY_WINDOW` (default 10) messages. When window overflows, the first `SHORT_TERM_COMPRESS_FIRST_N` messages are LLM-compressed into a summary.

```mermaid
flowchart TD
    Msg["New message — count > 10?"]
    Msg -->|No| Window["Return last 10 messages"]
    Msg -->|Yes| Compress["LiteLLM: memory_compression prompt\nresponse_format=MemoryCompressionOutput"]
    Compress --> Replace["messages[0:4] → summary_msg + messages[4:]"]
    Replace --> Persist["INSERT conversation_summaries"]
    Persist --> SSE2["event: compacting → frontend banner"]
    Replace --> Window
```

### Long-Term (per user)

After every response, an async fire-and-forget task extracts personal facts and preferences. Loaded at start of every query and injected into all agent contexts.

```mermaid
flowchart LR
    Response["Assistant response done"] --> Task["asyncio.create_task\nfire-and-forget"]
    Task --> LLM2["LiteLLM: long_term_memory_extraction\nresponse_format=LongTermMemoryExtraction"]
    LLM2 -->|should_store=false| NoOp["Skip"]
    LLM2 -->|should_store=true| Upsert["UPSERT user_long_term_memory\ncritical_facts + preferences"]
    Upsert --> NextQuery["Available in all agents\nnext conversation"]
```

---

## Tool System

`tools/registry.py` — singleton `ToolRegistry`. Auto-discovers all `connectors/*/tools.py` at startup.

```python
@tool(description="Search scraped website collections", requires_hitl=False, connector="__website__")
async def collection_search(query: str, collection_ids: list, user_id: str, top_k: int = 5) -> dict:
    ...
```

| Attribute | Purpose |
|-----------|---------|
| `description` | Injected into Master Agent prompt so it knows what the tool does |
| `requires_hitl` | `True` → pauses execution, shows HITL popup |
| `connector` | Non-empty → inject tokens from `connector_tokens_db[slug]` at call time |

**Server-injected parameters** (never sent to LLM):

| Parameter | Injected by | Source |
|-----------|-------------|--------|
| `access_token`, `instance_url` | OAuth connector slug match | `connector_instances.encrypted_tokens` |
| `kb_ids`, `user_id` | `connector="__kb__"` | `agent_knowledge_bases` rows |
| `collection_ids`, `user_id` | `connector="__website__"` | `agent_website_collections` rows |

---

## Connectors

### OAuth2 Connectors

| Connector | Auth | Tools |
|-----------|------|-------|
| Gmail | OAuth2 (Google) | `gmail_read_mail`, `gmail_send_mail` (HITL), `gmail_create_draft`, `gmail_list_labels` |
| GitHub | OAuth2 | `github_list_repos`, `github_list_issues`, `github_create_issue` (HITL), `github_list_pull_requests` |
| Google Calendar | OAuth2 (Google) | `calendar_list_events`, `calendar_create_event` (HITL), `calendar_delete_event` (HITL) |
| Salesforce | OAuth2 | `salesforce_query`, `salesforce_get_record`, `salesforce_create_record` (HITL), `salesforce_update_record` (HITL) |

### API-Key Connectors (always available)

| Connector | Tools |
|-----------|-------|
| Web Search (Tavily) | `web_search`, `web_search_news`, `fetch_url` |

### Implicit Connectors (auto-injected per agent)

| Connector slug | Tool | Injection source |
|----------------|------|-----------------|
| `__kb__` | `knowledge_base_search` | Agent's assigned knowledge bases |
| `__website__` | `collection_search` | Agent's assigned website collections |

---

## LiteLLM Multi-Provider Keys

| Prefix | Provider |
|--------|----------|
| `sk-ant-` | Anthropic |
| `sk-` | OpenAI |
| `AIza` | Gemini |
| `gsk_` | Groq |
| `AP` | Mistral |

---

## Authentication & Security

| Mechanism | Detail |
|-----------|--------|
| Password hashing | argon2-cffi, `check_needs_rehash()` on login |
| JWT | python-jose, HS256, 15min access + 7d refresh |
| Refresh tokens | SHA-256 hash in DB, raw token to client |
| Logout blacklist | Access token hash in Redis with TTL = remaining validity |
| Encryption | AES-256-GCM for connector tokens + API keys |
| Ownership | Every manager method filters by `user_id` from JWT |

---

## API Endpoints

### Auth — `/auth`

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST` | `/auth/register` | Public | Returns access + refresh tokens |
| `POST` | `/auth/login` | Public | Returns access + refresh tokens |
| `POST` | `/auth/refresh` | Bearer | Returns new access + refresh tokens |
| `POST` | `/auth/logout` | Bearer | Blacklists access token in Redis |
| `GET` | `/auth/me` | Bearer | Current user profile |
| `GET` | `/auth/me/stats` | Bearer | 8 counters: workspaces, agents, conversations, messages, total_cost_usd, knowledge_bases, website_collections, active_connectors |
| `GET` | `/auth/me/recent-conversations` | Bearer | `?limit=` (1–50). Latest conversations across all workspaces |

### Workspaces — `/workspaces`

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/workspaces` | Cursor paginated |
| `POST` | `/workspaces` | Auto-creates Master + Composer agents |
| `GET` | `/workspaces/{id}` | |
| `PUT` | `/workspaces/{id}` | |
| `DELETE` | `/workspaces/{id}` | Soft delete |

### Agents — `/agents`

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/workspaces/{id}/agents` | Returns `kb_ids`, `collection_ids` per agent |
| `POST` | `/workspaces/{id}/agents` | Accepts `kb_ids`, `collection_ids` |
| `PUT` | `/agents/{id}` | Updates KB/WC assignments via junction tables |
| `DELETE` | `/agents/{id}` | Soft delete |
| `POST` | `/workspaces/{id}/agents/prompt-generate` | AI prompt + tool suggestions |

### Knowledge Bases — `/knowledge-bases`

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/knowledge-bases` | Create KB |
| `GET` | `/knowledge-bases` | List user's KBs |
| `DELETE` | `/knowledge-bases/{kb_id}` | Delete KB + Qdrant collection |
| `POST` | `/knowledge-bases/{kb_id}/upload` | Upload files (multipart) — enqueues Celery task per file |
| `GET` | `/knowledge-bases/{kb_id}/documents` | List documents with status |
| `DELETE` | `/knowledge-bases/{kb_id}/documents/{doc_id}` | Delete doc + Qdrant chunks |
| `POST` | `/knowledge-bases/{kb_id}/documents/{doc_id}/retry` | Re-queue failed document |
| `GET` | `/knowledge-bases/status/stream` | SSE — `?token=` auth |

### Website Collections — `/website-collections`

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/website-collections` | Create collection |
| `GET` | `/website-collections` | List user's collections |
| `DELETE` | `/website-collections/{collection_id}` | Delete + Qdrant collection |
| `POST` | `/website-collections/{collection_id}/urls` | Add URL — body: `{url, max_depth}` |
| `GET` | `/website-collections/{collection_id}/urls` | List URLs with crawl status |
| `DELETE` | `/website-collections/{collection_id}/urls/{url_id}` | Remove URL + Qdrant chunks |
| `POST` | `/website-collections/{collection_id}/urls/{url_id}/scrape` | Trigger crawl for one URL |
| `POST` | `/website-collections/{collection_id}/scrape` | Trigger crawl for all URLs |
| `POST` | `/website-collections/{collection_id}/urls/{url_id}/retry` | Re-queue failed URL |
| `GET` | `/website-collections/status/stream` | SSE — `?token=` auth — registered before `/{collection_id}` |

### Connectors — `/connectors`

| Method | Path |
|--------|------|
| `GET` | `/connectors/definitions` |
| `GET` | `/connectors/instances` |
| `GET` | `/connectors/{slug}/auth-url` |
| `GET` | `/connectors/callback` |
| `DELETE` | `/connectors/instances/{id}` |

### API Keys — `/api-keys`

| Method | Path |
|--------|------|
| `POST` | `/api-keys` |
| `GET` | `/api-keys` |
| `GET` | `/api-keys/{id}/models` |
| `DELETE` | `/api-keys/{id}` |

### Chat — `/chat`

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/chat/conversations` | Create for workspace |
| `GET` | `/chat/conversations` | Cursor paginated (`?cursor=&limit=`) |
| `GET` | `/chat/conversations/{id}/messages` | Cursor paginated — `?cursor=&limit=` — returns `{messages, has_more, prev_cursor}`. Send `prev_cursor` back as `?cursor=` to load older messages (scroll-up pagination) |
| `POST` | `/chat/artifacts` | Save PDF or CSV artifact to B2 storage. Body: `{message_id, conversation_id, type, title, filename, content}`. Returns `{id, url}`. Idempotent — returns existing artifact if already saved for that `message_id + type` |
| `POST` | `/chat/stream` | Main SSE execution endpoint |
| `POST` | `/hitl/respond` | Approve/deny → Redis publish |

### Personas

| Method | Path |
|--------|------|
| `POST` | `/personas` |
| `GET` | `/personas` |
| `DELETE` | `/personas/{id}` |

### Admin — `/admin` (role=admin required)

Non-admin users are redirected to index. All list endpoints support `?cursor=&limit=`.

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/admin/stats` | System-wide totals: users, workspaces, conversations, messages |
| `PATCH` | `/admin/users/{id}` | Toggle `role` (admin/user) or `is_active` |

**Cursor-paginated tables** (returns `{items, next_cursor, has_next}`):

| Endpoint | Columns returned |
|----------|-----------------|
| `GET /admin/users` | id, email, role, is_active, created_at |
| `GET /admin/workspaces` | id, user_id, name, created_at |
| `GET /admin/conversations` | id, user_id, workspace_id, title, created_at |
| `GET /admin/agents` | id, workspace_id, name, agent_type, model_id, deleted_at, created_at |
| `GET /admin/personas` | id, user_id, name, created_at |
| `GET /admin/messages` | id, conversation_id, role, content (first 100 chars), total_cost_usd, latency_ms, created_at |
| `GET /admin/conversation-summaries` | id, conversation_id, message_range_start, message_range_end, created_at |
| `GET /admin/hitl-requests` | id, conversation_id, agent_id, tool_names, status, expires_at, created_at |
| `GET /admin/message-artifacts` | id, message_id, user_id, type, title, filename, created_at |
| `GET /admin/knowledge-bases` | id, user_id, name, document_count, created_at |
| `GET /admin/kb-documents` | id, kb_id, filename, processing_status, chunk_count, created_at |
| `GET /admin/website-collections` | id, user_id, name, url_count, created_at |
| `GET /admin/website-urls` | id, collection_id, url, crawl_status, page_count, chunk_count, created_at |
| `GET /admin/connector-definitions` | id, slug, display_name, auth_type, is_active, created_at |
| `GET /admin/connector-instances` | id, user_id, definition_id, account_label, status, token_expires_at, created_at |
| `GET /admin/api-keys` | id, user_id, key_name, provider, created_at |
| `GET /admin/long-term-memory` | id, user_id, critical_facts (truncated), preferences (truncated), updated_at |
| `GET /admin/refresh-tokens` | id, user_id, expires_at, revoked_at, created_at |

**Junction tables** (`?limit=500`, no cursor):

| Endpoint | Columns |
|----------|---------|
| `GET /admin/agent-kbs` | agent_id, kb_id |
| `GET /admin/agent-personas` | agent_id, persona_id |
| `GET /admin/agent-website-collections` | agent_id, collection_id |

---

## User Dashboard & Admin Panel

### User Dashboard (`/dashboard.html`)

Personal stats page, no admin role required.

- **8 stat cards**: Workspaces · Agents · Conversations · Messages · Cost ($) · Knowledge Bases · Web Collections · Active Connectors
- **Recent conversations**: last 8 across all workspaces — workspace name, time ago, direct Open link
- **Connected services**: active OAuth connectors
- **My Workspaces**: Chat and Build buttons per workspace
- **Personas**: inline CRUD (create, delete)

Stats sourced from `GET /auth/me/stats`. Admin link shown only when `role=admin`.

### Admin Panel (`/admin.html`)

`role=admin` required — non-admins are redirected to index on load.

```
┌──────────────────────────────────────────────────────────────┐
│  ← Dashboard    Cortex Admin                  admin@x.com   │
├──────────┬────────────┬────────────┬──────────┬─────────────┤
│ Users:42 │ Workspaces │ Convs:305  │ Msgs:9k  │             │
├──────────┴────────────┴────────────┴──────────┴─────────────┤
│  [Users][Workspaces][Agents][Personas][Conversations]        │
│  [Messages][Conv Summaries][HITL][Artifacts][KBs][KB Docs]  │
│  [Web Cols][Web URLs][Connector Defs][Connectors][API Keys]  │
│  [LTM][Refresh Tokens][Agent↔KBs][Agent↔Personas][Agent↔WCs]│
├─────────────────────────────────────────────────────────────┤
│  <table rows>                             [Load More] 50 rows│
└─────────────────────────────────────────────────────────────┘
```

- 21 tab buttons — one per DB table; cursor-paginated with Load More
- **Users tab only**: `→ Admin / → User` role toggle + `Activate / Deactivate` buttons
- Junction tables (agent-kbs, agent-personas, agent-website-collections) load flat with no cursor

---

## Database Schema

```mermaid
erDiagram
    users {
        UUID id PK
        VARCHAR email UK
        VARCHAR hashed_password
        ENUM role
        BOOL is_active
    }

    user_api_keys {
        UUID id PK
        UUID user_id FK
        VARCHAR key_name
        TEXT encrypted_key
        VARCHAR provider
        JSONB available_models
    }

    workspaces {
        UUID id PK
        UUID user_id FK
        VARCHAR name
        TIMESTAMPTZ deleted_at
    }

    agents {
        UUID id PK
        UUID workspace_id FK
        UUID user_id FK
        VARCHAR name
        TEXT system_prompt
        ENUM agent_type
        VARCHAR model_id
        UUID api_key_id FK
        INT display_order
        BOOL is_editable
        JSONB tools_config
        TIMESTAMPTZ deleted_at
    }

    knowledge_bases {
        UUID id PK
        UUID user_id FK
        VARCHAR name
        TEXT description
        INT doc_count
    }

    kb_documents {
        UUID id PK
        UUID kb_id FK
        UUID user_id FK
        VARCHAR filename
        ENUM processing_status
        INT chunk_count
        TEXT error_message
        TIMESTAMPTZ processed_at
    }

    agent_knowledge_bases {
        UUID agent_id PK
        UUID kb_id PK
    }

    website_collections {
        UUID id PK
        UUID user_id FK
        VARCHAR name
        TEXT description
        INT url_count
    }

    website_urls {
        UUID id PK
        UUID collection_id FK
        UUID user_id FK
        TEXT url
        INT max_depth
        ENUM crawl_status
        INT page_count
        INT chunk_count
        INT login_blocked_count
        TEXT error_message
        TIMESTAMPTZ last_crawled_at
    }

    agent_website_collections {
        UUID agent_id PK
        UUID collection_id PK
    }

    conversations {
        UUID id PK
        UUID workspace_id FK
        UUID user_id FK
        VARCHAR title
    }

    messages {
        UUID id PK
        UUID conversation_id FK
        ENUM role
        TEXT content
        JSONB token_details
        FLOAT total_cost_usd
        INT latency_ms
        VARCHAR langfuse_trace_id
    }

    message_artifacts {
        UUID id PK
        UUID message_id FK
        UUID conversation_id FK
        UUID user_id FK
        VARCHAR type
        VARCHAR title
        VARCHAR filename
        TEXT storage_key
        TIMESTAMPTZ created_at
    }

    conversation_summaries {
        UUID id PK
        UUID conversation_id FK
        TEXT summary
        INT message_range_start
        INT message_range_end
        TIMESTAMPTZ created_at
    }

    personas {
        UUID id PK
        UUID user_id FK
        VARCHAR name
        TEXT description
        TEXT system_prompt
        TIMESTAMPTZ created_at
    }

    refresh_tokens {
        UUID id PK
        UUID user_id FK
        TEXT token_hash
        TIMESTAMPTZ expires_at
        TIMESTAMPTZ revoked_at
        TIMESTAMPTZ created_at
    }

    connector_instances {
        UUID id PK
        UUID user_id FK
        UUID definition_id FK
        TEXT encrypted_tokens
        TIMESTAMPTZ token_expires_at
        VARCHAR account_label
        VARCHAR status
        TIMESTAMPTZ created_at
    }

    hitl_requests {
        UUID id PK
        UUID conversation_id FK
        VARCHAR agent_id
        JSONB tool_names
        ENUM status
        TEXT user_instructions
        TIMESTAMPTZ expires_at
    }

    user_long_term_memory {
        UUID id PK
        UUID user_id FK "UK"
        JSONB critical_facts
        JSONB preferences
    }

    users ||--o{ user_api_keys : has
    users ||--o{ workspaces : owns
    users ||--o{ knowledge_bases : owns
    users ||--o{ website_collections : owns
    users ||--o{ personas : owns
    users ||--o{ refresh_tokens : has
    users ||--|| user_long_term_memory : has
    workspaces ||--o{ agents : contains
    workspaces ||--o{ conversations : has
    agents }o--o{ knowledge_bases : uses
    agents }o--o{ website_collections : uses
    agents ||--o{ agent_knowledge_bases : has
    agents ||--o{ agent_website_collections : has
    agents ||--o{ agent_personas : has
    knowledge_bases ||--o{ kb_documents : contains
    knowledge_bases ||--o{ agent_knowledge_bases : linked_via
    website_collections ||--o{ website_urls : contains
    website_collections ||--o{ agent_website_collections : linked_via
    personas ||--o{ agent_personas : linked_via
    conversations ||--o{ messages : has
    conversations ||--o{ conversation_summaries : has
    conversations ||--o{ hitl_requests : has
    messages ||--o{ message_artifacts : has
```
---

## Celery Background Tasks

| Task | Module | Max Retries | Timeout |
|------|--------|-------------|---------|
| `ingest_document_task` | `document_pipeline.tasks` | 2 | `WC_CRAWL_TIMEOUT_SECONDS + 60` |
| `crawl_website_task` | `web_pipeline.tasks` | 2 | `WC_CRAWL_TIMEOUT_SECONDS + 60` (630s) |

Both tasks:
- `acks_late=True` — message re-queued if worker dies mid-task
- `task_reject_on_worker_lost=True` — nack on worker crash
- `ValueError` = non-retriable (missing record, login_required) — re-raised without retry
- All other exceptions → `self.retry(exc=exc)` up to `max_retries`
- `on_failure` handler → DB `status=failed` + Redis PUBLISH → SSE status update

---

## Retry & Resilience

All LLM calls and Redis operations use `tenacity`. Settings from `config/settings.py` — no magic numbers.

```python
# LLM calls
@retry(
    stop=stop_after_attempt(settings.LLM_MAX_RETRIES),       # default 3
    wait=wait_exponential_jitter(
        initial=settings.LLM_RETRY_WAIT_MIN,                 # 1.0s
        max=settings.LLM_RETRY_WAIT_MAX,                     # 30.0s
        jitter=settings.LLM_RETRY_JITTER,                    # 2.0s
    ),
    retry=retry_if_exception(_is_retriable),                 # rate/timeout/5xx
)

# Redis ops
@retry(
    stop=stop_after_attempt(settings.REDIS_MAX_RETRIES),     # default 2
    wait=wait_fixed(settings.REDIS_RETRY_WAIT_FIXED),        # 0.5s
)
```

---

## All Configuration Settings

`config/settings.py` — pydantic-settings loaded from `.env`.

### Core

| Setting | Default | Purpose |
|---------|---------|---------|
| `ENVIRONMENT` | `dev` | `dev` = debug logging, open CORS, Swagger; `prod` = restricted |
| `DEFAULT_MODEL` | `gpt-4o` | Fallback model when agent has no key |
| `DATABASE_URL` | `postgresql+asyncpg://...` | Async PostgreSQL URL |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis for HITL + OAuth + budget |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` | Celery broker + backend |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant vector database |

### Auth & Security

| Setting | Default | Purpose |
|---------|---------|---------|
| `JWT_SECRET` | — | HS256 signing key |
| `ENCRYPTION_KEY` | — | AES-256-GCM key, base64url, ≥ 32 bytes |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | JWT access token TTL |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token TTL |
| `CORS_ORIGINS` | `[]` | Prod CORS whitelist |

### Knowledge Base

| Setting | Default | Purpose |
|---------|---------|---------|
| `KB_EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model |
| `KB_EMBEDDING_DIMS` | `1536` | Vector dimensions |
| `KB_EMBED_BATCH_SIZE` | `96` | Texts per embedding batch |
| `KB_CHUNK_SIZE` | `1000` | Tokens per chunk |
| `KB_CHUNK_OVERLAP` | `200` | Overlap between chunks |
| `KB_CSV_ROWS_PER_CHUNK` | `100` | CSV rows per chunk |
| `KB_STAGING_DIR` | `./staging` | Local file staging path |
| `KB_TOP_K_DENSE` | `50` | Dense search candidates |
| `KB_TOP_K_SPARSE` | `50` | Sparse (BM25) search candidates |
| `KB_TOP_K_RRF` | `20` | After RRF merge |
| `KB_TOP_K_FINAL` | `5` | Final chunks returned to agent |
| `KB_RRF_K` | `60` | RRF rank constant |
| `KB_MAX_FILES_PER_UPLOAD` | `50` | Max files per upload request |
| `KB_MAX_FILE_SIZE_MB` | `100` | Max single file size |

### Website Collections

| Setting | Default | Purpose |
|---------|---------|---------|
| `WC_MAX_URLS_PER_COLLECTION` | `50` | URL limit per collection |
| `WC_MAX_DEPTH` | `5` | Max crawl depth |
| `WC_CRAWL_TIMEOUT_SECONDS` | `600` | Spider subprocess timeout |
| `WC_MAX_PAGES_PER_URL` | `500` | Max pages crawled per URL |
| `WC_CONCURRENT_REQUESTS` | `8` | Scrapy concurrent requests |
| `WC_DOWNLOAD_TIMEOUT` | `30` | Per-request timeout (seconds) |
| `WC_OBEY_ROBOTS` | `true` | Respect robots.txt |
| `WC_USER_AGENT` | `CortexBot/1.0` | Crawler user agent |
| `WC_TOP_K_DENSE` | `50` | Dense search candidates |
| `WC_TOP_K_FINAL` | `5` | Final chunks returned to agent |

### Memory & Features

| Setting | Default | Purpose |
|---------|---------|---------|
| `SHORT_TERM_MEMORY_WINDOW` | `10` | Sliding window size |
| `SHORT_TERM_COMPRESS_FIRST_N` | `4` | Messages compressed when full |
| `ENABLE_SUGGESTIONS` | `true` | Follow-up question chips |
| `HITL_TIMEOUT_SECONDS` | `120` | Auto-deny HITL after |
| `TOKEN_BUDGET_ENABLED` | `true` | Daily/monthly limits |
| `USER_DAILY_TOKEN_BUDGET` | `100000` | Per-user daily limit |
| `USER_MONTHLY_TOKEN_BUDGET` | `2000000` | Per-user monthly limit |

### Storage (B2 / S3)

| Setting | Default | Purpose |
|---------|---------|---------|
| `B2_ENDPOINT` | `""` | Backblaze B2 or S3 endpoint |
| `B2_REGION` | `us-east-005` | Region |
| `B2_ACCESS_KEY_ID` | `""` | Access key |
| `B2_SECRET_ACCESS_KEY` | `""` | Secret key |
| `B2_BUCKET` | `""` | Bucket name |
| `B2_PRESIGN_EXPIRY` | `300` | Presigned URL TTL (seconds) |

---

## Observability

All LiteLLM calls auto-traced to Langfuse via `litellm.success_callback = ["langfuse"]` set at startup. Per-call metadata for trace grouping:

```python
metadata={"trace_name": "dynamic_agent_ResearchAgent", "trace_session_id": str(conversation_id)}
```

`langfuse_trace_id` stored on every assistant message row for feedback linking.

---

## Project Structure

```
cortex_app/
├── app/
│   ├── auth/                   # JWT auth, argon2 hashing, RBAC
│   ├── workspaces/             # Workspace CRUD
│   ├── agents/                 # Agent CRUD + AI prompt generator + KB/WC junction
│   ├── connectors/             # OAuth flow, AES-256 token encryption
│   ├── api_keys/               # LiteLLM key management, provider detection
│   ├── personas/               # User personas
│   ├── chat/                   # Conversations, messages, HITL, SSE streaming
│   ├── knowledge_bases/        # KB CRUD, document management
│   ├── website_collections/    # WC CRUD, URL management, scrape triggers
│   ├── admin/                  # Admin 21-table data explorer + PATCH /users/{id}
│   └── common/                 # api_response, exceptions, middleware, redis_client
├── core/
│   ├── schemas.py              # AgentInput, AgentOutput, ExecutionPlan
│   ├── dependency_resolver.py  # Kahn's topological sort
│   ├── orchestrator.py         # Stage execution + HITL + KB/WC token injection
│   ├── master_agent.py         # Plan generator (structured output)
│   ├── composer_agent.py       # Response synthesizer + artifacts
│   ├── dynamic_agent.py        # Executes agents with tools + server-injected params
│   ├── memory_manager.py       # Short-term + long-term memory
│   └── title_generator.py      # Async conversation title generation
├── connectors/
│   ├── gmail/                  # GmailConnector + tool functions
│   ├── github/                 # GitHubConnector + tool functions
│   ├── calendar/               # CalendarConnector + tool functions
│   ├── salesforce/             # SalesforceConnector + tool functions
│   ├── tavily/                 # web_search, web_search_news, fetch_url
│   └── website_search/         # collection_search (connector="__website__")
├── document_pipeline/
│   ├── tasks.py                # ingest_document_task (Celery)
│   ├── parsers.py              # PDF, DOCX, CSV, image parsers
│   ├── chunker.py              # Overlapping text chunker
│   ├── embedder.py             # OpenAI embedding batches
│   └── vector_store.py         # Qdrant ops: kb_{id}, dense+sparse+RRF
├── web_pipeline/
│   ├── tasks.py                # crawl_website_task (Celery)
│   ├── spider.py               # Scrapy CortexSpider, login-wall detection
│   ├── vector_store.py         # Qdrant ops: wc_{id}, dense only
│   └── retriever.py            # Dense search + multi-collection merge
├── tools/
│   └── registry.py             # ToolRegistry singleton, auto-discovery
├── config/
│   └── settings.py             # All settings via pydantic-settings
├── database/
│   └── session.py              # Async SQLAlchemy engine + session factory
├── frontend/
│   ├── auth.html               # Login / register
│   ├── index.html              # Workspace card grid
│   ├── workspace.html          # Agent builder + KB/WC pickers
│   ├── chat.html               # SSE chat + HITL popup + artifact save + scroll pagination
│   ├── knowledge-bases.html    # Two-panel KB manager + SSE status
│   ├── website-collections.html # Two-panel WC manager + SSE status
│   ├── dashboard.html          # User dashboard: 8 stat cards, recent convs, connectors, workspaces, personas
│   └── admin.html              # Admin data explorer — all 21 DB tables, cursor pagination, user management
├── alembic/
│   └── versions/
│       ├── v001_initial_schema.py
│       ├── v002_knowledge_bases.py
│       ├── v003_website_collections.py
│       ├── v004_message_artifacts.py
│       └── v005_connector_token_expiry.py
├── main.py                     # FastAPI app, lifespan, router registration
├── celery_app.py               # Celery app: document_pipeline + web_pipeline tasks
├── seed_langfuse.py            # One-time prompt seeding
├── docker-compose.yml
└── requirements.txt
```

---

## Failure Handling

| Failure | Behaviour |
|---------|-----------|
| Master bad JSON | `PlanValidationError` → `error` SSE |
| Unknown agent name in plan | `error` SSE, execution stops |
| Circular dependency | `CircularDependencyError` → `error` SSE |
| Agent LLM error | `AgentOutput.error` set, execution continues |
| HITL timeout | Auto-denied, Composer notes missing output |
| KB ingest failure | `status=failed`, `error_message` set, Retry available |
| WC crawl failure | `status=failed`, `error_message` set, Retry available |
| WC login wall | `error_message` starts with `login_required:`, UI shows Remove (no Retry) |
| WC spider timeout | `RuntimeError` → retry up to 2× → `status=failed` |
| Duplicate agent name | `409 Conflict` |
| `TAVILY_API_KEY` not set | Tool raises `RuntimeError` with clear message |
| Qdrant unavailable | KB/WC search returns `[]`, agent continues without results |
| Redis down | HITL fails → `error` SSE; KB/WC status updates silently dropped |
