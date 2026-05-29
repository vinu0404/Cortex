# Cortex — Multi-Agent Orchestration Platform

A workspace-based multi-agent AI platform built with FastAPI and LiteLLM. Users build workspaces containing custom agents, connect them to real-world tools (Gmail, GitHub, Calendar, Salesforce, Web Search), and chat with the assembled workspace. Agents are orchestrated via **Kahn's topological sort** — independent agents run in parallel, dependent agents run in sequence. No LangChain, no LangGraph.

**Key design:** Each workspace has a non-editable **Master Agent** that plans execution (which agents, in what order, with what tools) and a non-editable **Composer Agent** that synthesizes the final streamed answer. Users define custom agents in between.

---

## How to Run

### Prerequisites

- **Python 3.11+**
- **Docker** (for PostgreSQL, Redis, Qdrant)
- A **Langfuse** account (cloud or self-hosted) — all prompts live there; app fails to start without it
- At least one LLM API key: OpenAI (`sk-`), Anthropic (`sk-ant-`), Gemini (`AIza`), or Groq (`gsk_`)

### 1. Start Infrastructure

```bash
docker compose up -d
```

| Service | Port | Purpose |
|---------|------|---------|
| PostgreSQL 16 | `5432` | All relational data — users, workspaces, agents, messages, HITL |
| Redis 7 | `6379` | HITL pub/sub, OAuth CSRF state, token budget counters |
| Qdrant | `6333` | Reserved for Phase 2 knowledge bases |

### 2. Create Virtual Environment

```bash
python -m venv .venv
source .venv/bin/activate          # Linux / macOS
.\.venv\Scripts\activate           # Windows

pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create `.env` in `cortex_app/`:

```env
# ── Required ──────────────────────────────────────────────────────
DATABASE_URL=postgresql+asyncpg://cortex:cortex@localhost:5432/cortex
REDIS_URL=redis://localhost:6379/0

# ── Auth ──────────────────────────────────────────────────────────
JWT_SECRET=your-256-bit-secret
ENCRYPTION_KEY=your-32-byte-base64-encoded-key     # base64url, decodes to ≥ 32 bytes

# ── Langfuse (required) ───────────────────────────────────────────
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com

# ── Web Search (optional) ─────────────────────────────────────────
TAVILY_API_KEY=tvly-...

# ── OAuth Connectors (optional) ───────────────────────────────────
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://localhost:8000/connectors/callback

GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
GITHUB_REDIRECT_URI=http://localhost:8000/connectors/callback

SALESFORCE_CLIENT_ID=...
SALESFORCE_CLIENT_SECRET=...
SALESFORCE_REDIRECT_URI=http://localhost:8000/connectors/callback

# ── Env toggle ────────────────────────────────────────────────────
ENVIRONMENT=dev       # dev = DEBUG logging, open CORS, Swagger UI
                      # prod = INFO logging, CORS_ORIGINS whitelist, no Swagger
```

### 4. Seed Langfuse Prompts

All agent prompts (Master, Composer, memory compression, title generation, suggestion generation, long-term memory extraction) live in Langfuse. Seed them once:

```bash
python seed_langfuse.py
```

Required prompt names:
- `master_agent`
- `composer_agent`
- `memory_compression`
- `long_term_memory_extraction`
- `title_generation`
- `suggestion_generation`
- `agent_prompt_generator`

### 5. Apply Database Migrations

```bash
alembic upgrade head
```

**Everyday workflow** after changing a DB model:

```bash
alembic revision --autogenerate -m "describe change"
# Review generated file in alembic/versions/
alembic upgrade head
```

```bash
alembic current          # which revision DB is at
alembic history          # full migration history
alembic downgrade -1     # roll back one migration
```

### 6. Start the Server

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

| URL | What |
|-----|------|
| `http://localhost:8000` | Workspace home |
| `http://localhost:8000/auth.html` | Login / register |
| `http://localhost:8000/workspace.html?id=<uuid>` | Agent builder |
| `http://localhost:8000/chat.html?workspace=<uuid>` | Chat UI |
| `http://localhost:8000/dashboard.html` | Usage stats |
| `http://localhost:8000/docs` | Swagger (dev only) |

### Quick Start Summary

```
Terminal 1:  docker compose up -d
             alembic upgrade head
             python seed_langfuse.py
Terminal 2:  uvicorn main:app --reload --port 8000
```

---

## System Architecture

```mermaid
graph TB
    User["User (Browser)"]

    User -->|"REST + SSE"| API["FastAPI Layer"]

    subgraph Startup ["Startup (lifespan)"]
        Seed["Seed connector definitions"]
        Discover["ToolRegistry.auto_discover()"]
        LFHook["LiteLLM → Langfuse callback"]
    end

    API -->|"POST /chat/stream"| Stream["SSE Streaming Pipeline"]

    Stream --> LoadCtx["Load workspace context<br/><i>agents, api keys, connector tokens</i>"]
    LoadCtx --> MemLoad["MemoryManager.load()<br/><i>summaries + last N messages</i>"]
    MemLoad --> Master["Master Agent<br/><i>LiteLLM + Langfuse prompt<br/>response_format=ExecutionPlan</i>"]

    Master -->|"ExecutionPlan (JSON)"| Validate["Plan validation<br/><i>agent names, dep IDs, no cycles</i>"]
    Validate --> Orch["Orchestrator<br/><i>Kahn toposort → stages</i>"]

    subgraph Stage1 ["Stage 1 (asyncio.gather)"]
        A1["Custom Agent A<br/><i>dynamic_agent + tools</i>"]
        A2["Custom Agent B<br/><i>dynamic_agent + tools</i>"]
    end

    subgraph Stage2 ["Stage 2 (waits for Stage 1)"]
        A3["Custom Agent C<br/><i>receives A+B outputs</i>"]
    end

    Orch --> Stage1
    Stage1 --> Stage2

    Stage1 --> SharedState["Shared State<br/><i>runtime_id → AgentOutput</i>"]
    Stage2 --> SharedState

    SharedState --> Composer["Composer Agent<br/><i>streams answer token by token</i>"]
    Composer -->|"SSE token events"| User

    Stream -.->|"asyncio.create_task"| TitleGen["Title Generator<br/><i>fire-and-forget</i>"]
    Stream -.->|"asyncio.create_task"| LTMem["Long-Term Memory Extractor<br/><i>fire-and-forget</i>"]

    subgraph Persistence
        PG[("PostgreSQL")]
        Redis[("Redis")]
    end

    API <--> PG
    Stream <--> Redis
    Master <--> Langfuse["Langfuse Cloud<br/><i>prompts + traces</i>"]
```

---

## Workspace Model

Each user creates **workspaces**. A workspace is a collection of agents that collaborate to answer queries.

```mermaid
graph LR
    subgraph Workspace ["Workspace (e.g. 'Sales Assistant')"]
        MA["🔒 Master Agent<br/><i>plans execution<br/>not editable</i>"]
        A1["Custom Agent 1<br/><i>ResearchAgent<br/>tools: web_search</i>"]
        A2["Custom Agent 2<br/><i>EmailAgent<br/>tools: gmail_send_mail</i>"]
        CA["🔒 Composer Agent<br/><i>synthesizes answer<br/>not editable</i>"]
    end

    Query["User query"] --> MA
    MA -->|"plan"| A1
    MA -->|"plan"| A2
    A1 -->|"results"| CA
    A2 -->|"results"| CA
    CA -->|"SSE stream"| Answer["Streamed answer"]
```

- **Master** and **Composer** are auto-created with every new workspace (`is_editable=false`)
- Custom agents have: name, system prompt, LLM model + API key, tools
- Agent names within a workspace are **unique** — Master references them by name in the plan
- Same agent can appear multiple times in one plan with different runtime IDs (`research_1`, `research_2`)

---

## Agent Builder

`workspace.html` — drag-and-drop agent canvas.

```
┌────────────────────────────────────────────────────────────────────┐
│  🔒 MASTER          [ResearchAgent]  [EmailAgent]  🔒 COMPOSER     │
│  (locked)           drag to reorder                    (locked)    │
├────────────────────────────────────────────────────────────────────┤
│  Sidebar                                                           │
│  ├── API Keys    [+ Add Key]  (auto-detects provider + models)     │
│  └── Connectors  [Gmail ✓ Connected] [GitHub ✓] [Web Search 🔵]   │
└────────────────────────────────────────────────────────────────────┘
```

**AI Prompt Generator:** Describe what an agent should do → backend calls LiteLLM → returns generated system prompt + recommended tools from user's connected connectors.

---

## Orchestration — Kahn's Topological Sort

`core/dependency_resolver.py` — Kahn's algorithm on the plan's `depends_on` edges.

### Pattern 1: Parallel (no dependencies)

> *"Search my emails AND look up latest news on our competitor"*

```mermaid
graph LR
    Master --> Orch

    subgraph "Stage 1 — parallel asyncio.gather"
        A["EmailAgent<br/><i>gmail_read_mail</i>"]
        B["WebAgent<br/><i>web_search</i>"]
    end

    Orch --> A & B
    A & B --> Composer
    style A fill:#4CAF50,color:#fff
    style B fill:#2196F3,color:#fff
```

### Pattern 2: Sequential (linear chain)

> *"Find sales data, analyze it with Python, email the report"*

```mermaid
graph LR
    Master --> Orch

    subgraph "Stage 1"
        A["DataAgent<br/><i>gmail_read_mail</i>"]
    end

    subgraph "Stage 2"
        B["AnalystAgent<br/><i>uses A output</i>"]
    end

    subgraph "Stage 3"
        C["EmailAgent<br/><i>gmail_send_mail<br/>uses B output</i>"]
    end

    Orch --> A --> B --> C --> Composer
    style A fill:#4CAF50,color:#fff
    style B fill:#FF9800,color:#fff
    style C fill:#9C27B0,color:#fff
```

### Pattern 3: Diamond (fan-out + fan-in)

> *"Search internal docs AND web, then synthesize a comparison"*

```mermaid
graph TD
    Master --> Orch

    subgraph "Stage 1 — parallel"
        A["DocAgent"]
        B["WebAgent<br/><i>web_search</i>"]
    end

    subgraph "Stage 2 — waits for both"
        C["AnalystAgent<br/><i>receives A+B outputs</i>"]
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
    Check -->|"Yes → cycle!"| Err["CircularDependencyError → error SSE"]
    Check -->|No| Done["Return List[List[ResolvedAgentTask]]"]

    style Err fill:#f44336,color:#fff
    style Done fill:#4CAF50,color:#fff
```

---

## SSE Streaming Events

`POST /chat/stream` — all real-time communication via Server-Sent Events.

```
event: plan          {"execution_order": "Master → ResearchAgent[web_search] → WriterAgent → Composer"}
event: status        {"phase": "planning"|"executing"|"composing", "agent_name": "ResearchAgent"}
event: agent_result  {"agent_id": "...", "agent_name": "...", "done": true, "error": null}
event: hitl_required {"request_id": "...", "agent_name": "...", "tool_names": ["gmail_send_mail"], "timeout_seconds": 120}
event: hitl_approved {"request_id": "..."}
event: hitl_denied   {"request_id": "..."}
event: compacting    {"message": "Summarising earlier conversation..."}
event: token         {"text": "..."}          ← streamed char-by-char from Composer
event: suggestions   {"questions": ["...", "...", "..."]}
event: done          {"total_ms": ..., "tokens": ..., "conversation_id": "..."}
event: error         {"message": "..."}
```

**Frontend uses `fetch` + `ReadableStream` (not `EventSource`)** — required because `EventSource` can't send JWT auth headers.

---

## Human-in-the-Loop (HITL)

Tools marked `requires_hitl=True` pause execution and ask the user to approve before the tool runs. HITL works **across multiple server workers** via Redis pub/sub — no sticky sessions needed.

```mermaid
sequenceDiagram
    participant SSE as SSE Stream
    participant Redis as Redis pub/sub
    participant User as User (Browser)
    participant HITL as POST /hitl/respond

    SSE->>SSE: Agent needs gmail_send_mail (requires_hitl=True)
    SSE->>Redis: Subscribe hitl:{request_id}
    SSE->>User: event: hitl_required { request_id, tool_names, timeout=120s }

    alt User approves
        User->>HITL: { request_id, approved: true, instructions: "only to john@" }
        HITL->>Redis: PUBLISH hitl:{request_id} {approved: true, instructions: ...}
        Redis->>SSE: Message received → resume
        SSE->>User: event: hitl_approved
    else User denies
        User->>HITL: { request_id, approved: false }
        Redis->>SSE: Message received
        SSE->>User: event: hitl_denied
        SSE->>SSE: AgentOutput.error = "HITL denied by user"
    else Timeout (120s)
        SSE->>SSE: Auto-deny, Composer synthesizes without that agent
    end
```

**HITL chat.html behavior:**
- Popup appears on `hitl_required` event with tool names, instructions textarea, countdown timer
- Approve / Deny buttons
- `pendingHitlId` captured before clearing popup to prevent null race

---

## Memory System

### Short-Term (per conversation)

Sliding window of `SHORT_TERM_MEMORY_WINDOW` (default 10) messages. When window overflows, first `SHORT_TERM_COMPRESS_FIRST_N` (default 4) messages are compressed via LLM into a summary stored in `conversation_summaries`.

```mermaid
flowchart TD
    Msg["New message added<br/><i>non-system count > 10?</i>"]
    Msg -->|No| Window["Return last 10 messages"]
    Msg -->|Yes| Compress["LiteLLM call<br/><i>Langfuse prompt: memory_compression<br/>response_format=MemoryCompressionOutput</i>"]
    Compress --> Replace["messages[0:4] → [summary_msg] + messages[4:]"]
    Replace --> Persist["INSERT conversation_summaries"]
    Persist --> SSE["event: compacting → frontend banner"]
    Replace --> Window
```

**Cold start:** `MemoryManager.load(summaries, recent_messages)` seeds from DB on first query of a conversation.

### Long-Term (per user)

After every response, a **fire-and-forget** `asyncio.create_task` runs an LLM that extracts personal facts from the exchange. Upserted to `user_long_term_memory`. Loaded at start of every query and injected into all agent contexts.

```mermaid
flowchart LR
    Response["Assistant response done"] --> Task["asyncio.create_task<br/><i>fire-and-forget</i>"]
    Task --> LLM["LiteLLM<br/><i>Langfuse: long_term_memory_extraction<br/>response_format=LongTermMemoryExtraction</i>"]
    LLM -->|"should_store=false"| NoOp["Skip"]
    LLM -->|"should_store=true"| Upsert["UPSERT user_long_term_memory<br/><i>critical_facts + preferences</i>"]
    Upsert --> NextQuery["Available in all agents<br/>next conversation"]
```

**What gets extracted:**

| Field | Example trigger |
|-------|----------------|
| `critical_facts.user_name` | *"I'm Vinay"* |
| `critical_facts.company_name` | *"I work at Acme Corp"* |
| `critical_facts.job_title` | *"I'm a product manager"* |
| `preferences.tone` | *"Keep answers concise"* |
| `preferences.detail_level` | *"Just give me the summary"* |

---

## Tool System

`tools/registry.py` — singleton `ToolRegistry`. Auto-discovers all `connectors/*/tools.py` files at startup.

```python
@tool(description="Search the web", requires_hitl=False, connector="")
async def web_search(query: str, num_results: int = 5) -> dict:
    ...
```

| Attribute | Purpose |
|-----------|---------|
| `description` | Injected into Master Agent prompt so it knows what the tool does |
| `requires_hitl` | `True` → pauses execution, shows HITL popup |
| `connector` | Non-empty → inject OAuth tokens from `connector_tokens_db[slug]` at call time |

**OAuth token injection:** Connector tools receive `access_token` (and `instance_url` for Salesforce) server-side, injected from the user's decrypted `connector_instances` row. The LLM never sees these credentials — they're added in `_execute_tool_calls` after the LLM decides to call the tool.

---

## Connectors

### OAuth2 Connectors (per-user)

Users connect their accounts once; tokens are encrypted (AES-256-GCM) and reused across all workspaces.

| Connector | Auth | Tools |
|-----------|------|-------|
| Gmail | OAuth2 (Google) | `gmail_read_mail`, `gmail_send_mail` *(HITL)*, `gmail_create_draft`, `gmail_list_labels` |
| GitHub | OAuth2 | `github_list_repos`, `github_list_issues`, `github_create_issue` *(HITL)*, `github_list_pull_requests` |
| Google Calendar | OAuth2 (Google) | `calendar_list_events`, `calendar_create_event` *(HITL)*, `calendar_delete_event` *(HITL)* |
| Salesforce | OAuth2 | `salesforce_query`, `salesforce_get_record`, `salesforce_create_record` *(HITL)*, `salesforce_update_record` *(HITL)* |

**OAuth CSRF protection:** State stored in Redis with 600s TTL. `getdel` on callback — replay attacks rejected.

### API-Key Connectors (system-level, always available)

| Connector | Auth | Tools |
|-----------|------|-------|
| Web Search (Tavily) | `TAVILY_API_KEY` in settings | `web_search`, `web_search_news`, `fetch_url` |

Tavily tools use `connector=""` — no token injection. Key read directly from `settings.TAVILY_API_KEY`. No "Connect" button in UI — shown as **Built-in**.

---

## LiteLLM Multi-Provider Keys

Users add their own API keys. The system auto-detects the provider from the key prefix and fetches available models.

| Prefix | Provider |
|--------|----------|
| `sk-ant-` | Anthropic |
| `sk-` | OpenAI |
| `AIza` | Gemini |
| `gsk_` | Groq |
| `AP` | Mistral |

Model discovery runs in `asyncio.get_running_loop().run_in_executor(None, ...)` (blocking HTTP call off the event loop). Models stored as JSONB in `user_api_keys.available_models`. Keys encrypted with AES-256-GCM, never returned after creation.

---

## Authentication & Security

| Mechanism | Detail |
|-----------|--------|
| Password hashing | **argon2-cffi** (`argon2.PasswordHasher`), not bcrypt |
| Rehash on login | `check_needs_rehash()` — upgrades old params transparently |
| JWT | `python-jose`, HS256, 15min access + 7d refresh |
| Refresh tokens | SHA-256 hash stored in DB, raw token to client |
| Logout blacklist | Access token hash in Redis with TTL = remaining validity |
| Ownership | Every manager method filters by `user_id` from JWT — cross-user access impossible |
| Encryption key | `ENCRYPTION_KEY` must base64-decode to ≥ 32 bytes; validated at startup |

---

## API Endpoints

### Auth — `/auth`

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST` | `/auth/register` | Public | Returns access + refresh tokens |
| `POST` | `/auth/login` | Public | Returns access + refresh tokens |
| `POST` | `/auth/refresh` | Bearer | Returns new access token |
| `POST` | `/auth/logout` | Bearer | Blacklists access token + revokes refresh |

### Workspaces — `/workspaces`

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/workspaces` | Cursor paginated |
| `POST` | `/workspaces` | Auto-creates Master + Composer agents |
| `GET` | `/workspaces/{id}` | Detail + agents |
| `PUT` | `/workspaces/{id}` | Name / description |
| `DELETE` | `/workspaces/{id}` | Soft delete |

### Agents — `/agents`

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/workspaces/{id}/agents` | List |
| `POST` | `/workspaces/{id}/agents` | CUSTOM type only; enforces name uniqueness |
| `PUT` | `/agents/{id}` | Guards `is_editable=true` |
| `DELETE` | `/agents/{id}` | Soft delete; blocks Master/Composer |
| `POST` | `/workspaces/{id}/agents/prompt-generate` | AI prompt + tool suggestions |

### Connectors — `/connectors`

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/connectors/definitions` | All seeded definitions (includes `auth_type`) |
| `GET` | `/connectors/instances` | User's connected instances |
| `GET` | `/connectors/{slug}/auth-url` | Start OAuth (oauth2 only) |
| `GET` | `/connectors/callback` | OAuth redirect callback |
| `DELETE` | `/connectors/instances/{id}` | Revoke + delete |

### API Keys — `/api-keys`

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/api-keys` | Provider detection + model listing |
| `GET` | `/api-keys` | Masked keys |
| `GET` | `/api-keys/{id}/models` | Available models |
| `DELETE` | `/api-keys/{id}` | |

### Personas — `/personas`

| Method | Path |
|--------|------|
| `POST` | `/personas` |
| `GET` | `/personas` |
| `PUT` | `/personas/{id}` |
| `DELETE` | `/personas/{id}` |

### Chat — `/chat`

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/chat/conversations` | Create for workspace |
| `GET` | `/chat/conversations` | Cursor paginated |
| `GET` | `/chat/conversations/{id}/messages` | History |
| `POST` | `/chat/stream` | **SSE** — main execution endpoint |
| `POST` | `/hitl/respond` | Approve/deny → Redis publish |

### Admin — `/admin`

| Method | Path | Auth |
|--------|------|------|
| `GET` | `/admin/users` | Admin role only |
| `GET` | `/admin/workspaces` | Admin role only |
| `GET` | `/admin/conversations` | Admin role only |
| `GET` | `/admin/stats` | Admin role only |

---

## Database Schema

```mermaid
erDiagram
    users {
        UUID id PK
        VARCHAR email UK
        VARCHAR hashed_password
        ENUM role "user|admin"
        BOOL is_active
    }

    refresh_tokens {
        UUID id PK
        UUID user_id FK
        VARCHAR token_hash
        TIMESTAMPTZ expires_at
        TIMESTAMPTZ revoked_at
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
        ENUM agent_type "MASTER|CUSTOM|COMPOSER"
        VARCHAR model_id
        UUID api_key_id FK
        INT display_order
        BOOL is_editable
        JSONB tools_config
        TIMESTAMPTZ deleted_at
    }

    connector_definitions {
        UUID id PK
        VARCHAR slug UK
        VARCHAR display_name
        ENUM auth_type "oauth2|apikey"
        JSONB tools
        VARCHAR icon
    }

    connector_instances {
        UUID id PK
        UUID user_id FK
        UUID definition_id FK
        TEXT encrypted_tokens
        VARCHAR account_label
        ENUM status "active|expired|revoked"
    }

    personas {
        UUID id PK
        UUID user_id FK
        VARCHAR name
        TEXT system_prompt
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
        ENUM role "user|assistant|system"
        TEXT content
        JSONB token_details
        FLOAT total_cost_usd
        INT latency_ms
        VARCHAR langfuse_trace_id
    }

    conversation_summaries {
        UUID id PK
        UUID conversation_id FK
        TEXT summary
        INT message_range_start
        INT message_range_end
    }

    hitl_requests {
        UUID id PK
        UUID conversation_id FK
        VARCHAR agent_id
        JSONB tool_names
        ENUM status "pending|approved|denied|timed_out"
        TEXT user_instructions
        TIMESTAMPTZ expires_at
    }

    user_long_term_memory {
        UUID id PK
        UUID user_id FK UK
        JSONB critical_facts
        JSONB preferences
    }

    users ||--o{ refresh_tokens : has
    users ||--o{ user_api_keys : has
    users ||--o{ workspaces : owns
    users ||--o{ connector_instances : has
    users ||--o{ personas : has
    users ||--|| user_long_term_memory : has
    workspaces ||--o{ agents : contains
    workspaces ||--o{ conversations : has
    conversations ||--o{ messages : has
    conversations ||--o{ conversation_summaries : has
    conversations ||--o{ hitl_requests : has
    connector_definitions ||--o{ connector_instances : instantiated_by
```

---

## Retry & Resilience

All LLM calls, external HTTP, and Redis operations use `tenacity`. All integers come from `config/settings.py` — no magic numbers in code.

```python
# LLM / HTTP
@retry(
    stop=stop_after_attempt(settings.LLM_MAX_RETRIES),          # default 3
    wait=wait_exponential_jitter(
        initial=settings.LLM_RETRY_WAIT_MIN,                    # 1.0s
        max=settings.LLM_RETRY_WAIT_MAX,                        # 30.0s
        jitter=settings.LLM_RETRY_JITTER,                       # 2.0s
    ),
    retry=retry_if_exception(_is_retriable),                    # rate/timeout/5xx
    reraise=True,
)

# Redis
@retry(
    stop=stop_after_attempt(settings.REDIS_MAX_RETRIES),        # default 2
    wait=wait_fixed(settings.REDIS_RETRY_WAIT_FIXED),           # 0.5s
)
```

---

## All Configuration Settings

`config/settings.py` — pydantic-settings, loaded from `.env`.

| Setting | Default | Purpose |
|---------|---------|---------|
| `ENVIRONMENT` | `"dev"` | `"dev"` \| `"prod"` |
| `DEFAULT_MODEL` | `"gpt-4o"` | Fallback model when agent has no key |
| `SHORT_TERM_MEMORY_WINDOW` | `10` | Max messages in sliding window |
| `SHORT_TERM_COMPRESS_FIRST_N` | `4` | Messages compressed when window full |
| `ENABLE_SUGGESTIONS` | `true` | Follow-up question chips after answer |
| `HITL_TIMEOUT_SECONDS` | `120` | Auto-deny HITL after this long |
| `TOKEN_BUDGET_ENABLED` | `true` | Daily/monthly token limits |
| `USER_DAILY_TOKEN_BUDGET` | `100,000` | Per-user daily limit |
| `USER_MONTHLY_TOKEN_BUDGET` | `2,000,000` | Per-user monthly limit |
| `LLM_MAX_RETRIES` | `3` | Tenacity attempts for LLM calls |
| `LLM_RETRY_WAIT_MIN` | `1.0` | Min backoff (seconds) |
| `LLM_RETRY_WAIT_MAX` | `30.0` | Max backoff (seconds) |
| `LLM_RETRY_JITTER` | `2.0` | Jitter added to backoff |
| `HTTP_MAX_RETRIES` | `3` | Tenacity attempts for external HTTP |
| `REDIS_MAX_RETRIES` | `2` | Tenacity attempts for Redis ops |
| `REDIS_RETRY_WAIT_FIXED` | `0.5` | Fixed wait between Redis retries |
| `LANGFUSE_PROMPT_CACHE_TTL` | `300` | Seconds to cache Langfuse prompts |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | JWT access token TTL |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token TTL |
| `CORS_ORIGINS` | `[]` | Allowed origins in prod (dev uses `*`) |
| `TAVILY_API_KEY` | `""` | Tavily web search (optional) |

---

## Observability

All LiteLLM calls are auto-traced to Langfuse via `CallbackHandler` registered at startup:

```python
langfuse_handler = CallbackHandler(public_key=..., secret_key=..., host=...)
litellm.callbacks = [langfuse_handler]
```

Per-call metadata passed for trace grouping:

```python
await litellm.acompletion(..., metadata={
    "trace_name": "master_agent",
    "trace_session_id": str(conversation_id),
    "trace_user_id": str(user_id),
    "tags": ["orchestration"],
})
```

`langfuse_trace_id` stored on every assistant `messages` row for feedback linking. `get_langfuse().flush()` called on shutdown to ensure no traces are lost.

---

## Project Structure

```
cortex_app/
├── app/
│   ├── auth/              # JWT auth, argon2 hashing, RBAC
│   ├── workspaces/        # Workspace CRUD, soft delete
│   ├── agents/            # Agent CRUD + AI prompt generator
│   ├── connectors/        # OAuth flow, instance management, AES-256 token encryption
│   ├── api_keys/          # LiteLLM key management, provider detection
│   ├── personas/          # User personas
│   ├── chat/              # Conversations, messages, HITL, SSE streaming
│   ├── admin/             # Admin-only views
│   └── common/            # api_response, exceptions, middleware, token_budget,
│                          # langfuse_client, redis_client, token_utils
├── core/
│   ├── schemas.py          # AgentInput, AgentOutput, ExecutionPlan, LongTermMemory
│   ├── dependency_resolver.py  # Kahn's algorithm
│   ├── orchestrator.py     # Stage-by-stage parallel execution + HITL callback
│   ├── master_agent.py     # Plan generator (Langfuse prompt, structured output)
│   ├── composer_agent.py   # Response synthesizer + suggestions
│   ├── dynamic_agent.py    # Executes user agents with tools + token injection
│   ├── memory_manager.py   # Short-term sliding window + long-term BG task
│   └── title_generator.py  # Async LLM title generation
├── connectors/
│   ├── base.py             # BaseConnector ABC (OAuth2 interface)
│   ├── gmail/              # GmailConnector + @tool functions
│   ├── github/             # GitHubConnector + @tool functions
│   ├── calendar/           # CalendarConnector + @tool functions
│   ├── salesforce/         # SalesforceConnector + @tool functions
│   └── tavily/             # web_search, web_search_news, fetch_url (no OAuth)
├── tools/
│   └── registry.py         # ToolRegistry singleton, auto-discovery, HITL list
├── config/
│   └── settings.py         # All settings via pydantic-settings
├── database/
│   └── session.py          # Async SQLAlchemy engine + session factory
├── frontend/
│   ├── auth.html           # Login / register
│   ├── index.html          # Workspace card grid
│   ├── workspace.html      # Agent builder canvas
│   ├── chat.html           # SSE chat + HITL popup + plan header
│   └── dashboard.html      # Stats (user + admin view)
├── alembic/                # DB migrations
├── main.py                 # FastAPI app, lifespan, router registration
├── seed_langfuse.py        # One-time prompt seeding
├── docker-compose.yml      # postgres + redis + qdrant + app + celery_worker
└── requirements.txt
```

---

## Failure Handling

| Failure | Behaviour |
|---------|-----------|
| Master agent bad JSON | Pydantic validation fails → `PlanValidationError` → `error` SSE event |
| Unknown agent name in plan | Caught in `_validate_plan` → `error` SSE, execution stops |
| Circular dependency | `CircularDependencyError` → `error` SSE, execution stops |
| Agent timeout / LLM error | `AgentOutput.error` set, stored in shared state; next stage continues |
| HITL timeout (120s) | Auto-denied; Composer synthesizes noting missing output |
| Redis down | HITL pub/sub fails → `error` SSE |
| Composer fails | Caught by try/except in streaming.py → `error` SSE |
| Duplicate agent name | `IntegrityError` → `409 Conflict` |
| `TAVILY_API_KEY` not set | Tool raises `RuntimeError` with clear message |

---

## What's in Phase 2

- **Composer artifacts** — persist generated files (PDFs, charts, CSVs) to S3 / Backblaze B2
- **Knowledge Bases** — file upload + web scrape → Qdrant embedding pipeline via Celery
- **Full OAuth token refresh** — auto-renew expired Gmail / GitHub / Calendar / Salesforce tokens
- **MCP servers** — user-supplied Model Context Protocol servers as tool sources
- **Cron jobs** — scheduled agent runs via Celery + RedBeat
