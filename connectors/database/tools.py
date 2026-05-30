"""
Database connector — SQL (PostgreSQL / MySQL) and MongoDB tools.

Auth: credentials type. Connection string stored as access_token in connector_instances.
Injection: dynamic_agent.py injects access_token + db_type at tool call time.
Token shape: {"access_token": "postgresql://user:pass@host/db", "db_type": "postgresql"}

Read-only guard: all SQL tools reject non-SELECT statements.
Sources: each tool returns a "sources" key — picked up by _extract_sources() automatically.
"""
import logging

from tools.registry import tool

logger = logging.getLogger(__name__)

_BLOCKED_SQL = frozenset({
    "insert", "update", "delete", "drop", "truncate",
    "alter", "create", "grant", "revoke", "exec", "execute",
})


def _assert_read_only(query: str) -> None:
    first = query.strip().split()[0].lower()
    if first in _BLOCKED_SQL:
        raise ValueError(f"'{first.upper()}' not allowed — only SELECT queries are permitted")


def _assert_safe_name(name: str) -> None:
    if not all(c.isalnum() or c in ("_", ".") for c in name):
        raise ValueError(f"Unsafe identifier '{name}'")


def _adapt_url(connection_string: str, db_type: str) -> str:
    """Normalise user-supplied connection string to SQLAlchemy async driver URL."""
    cs = connection_string.strip()
    if db_type == "postgresql":
        if cs.startswith("postgresql://") or cs.startswith("postgres://"):
            cs = cs.replace("postgresql://", "postgresql+asyncpg://", 1)
            cs = cs.replace("postgres://", "postgresql+asyncpg://", 1)
        return cs
    if db_type == "mysql":
        if cs.startswith("mysql://"):
            return cs.replace("mysql://", "mysql+aiomysql://", 1)
        return cs
    return cs  # mongodb — passed directly to motor


# ── SQL tools ─────────────────────────────────────────────────────────────────

@tool(
    description=(
        "Execute a read-only SQL SELECT query against the connected database. "
        "Returns columns and rows. Call list_tables first if unsure of schema. "
        "Only SELECT is allowed — INSERT/UPDATE/DELETE/DROP are blocked."
    ),
    requires_hitl=False,
    connector="database",
)
async def sql_query(
    query: str,
    access_token: str,
    db_type: str = "postgresql",
    limit: int = 100,
) -> dict:
    _assert_read_only(query)
    limit = min(limit, 500)

    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        import sqlalchemy as sa

        url = _adapt_url(access_token, db_type)
        engine = create_async_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    sa.text(f"SELECT * FROM ({query}) AS _cortex_q LIMIT :lim"),
                    {"lim": limit},
                )
                rows_raw = result.fetchall()
                columns = list(result.keys())
                rows = [list(r) for r in rows_raw]
        finally:
            await engine.dispose()
    except Exception as e:
        logger.error("sql_query failed: %s", e)
        return {"error": str(e), "query": query, "sources": []}

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "query": query,
        "sources": [
            {"type": "database", "title": f"SQL query returned {len(rows)} row(s)", "url": None}
        ],
    }


@tool(
    description=(
        "List all tables (PostgreSQL/MySQL) or collections (MongoDB) in the connected database. "
        "Always call this first before writing queries — do not guess table names."
    ),
    requires_hitl=False,
    connector="database",
)
async def list_tables(
    access_token: str,
    db_type: str = "postgresql",
) -> dict:
    if db_type == "mongodb":
        return await _mongo_list_collections(access_token)

    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        import sqlalchemy as sa

        url = _adapt_url(access_token, db_type)
        engine = create_async_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as conn:
                if db_type == "postgresql":
                    result = await conn.execute(sa.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' ORDER BY table_name"
                    ))
                else:  # mysql
                    result = await conn.execute(sa.text("SHOW TABLES"))
                tables = [row[0] for row in result.fetchall()]
        finally:
            await engine.dispose()
    except Exception as e:
        logger.error("list_tables failed: %s", e)
        return {"error": str(e), "tables": [], "sources": []}

    return {
        "tables": tables,
        "count": len(tables),
        "sources": [{"type": "database", "title": f"Database schema ({len(tables)} tables)", "url": None}],
    }


@tool(
    description=(
        "Return column names, data types, and nullable flags for a specific table. "
        "Use after list_tables to understand structure before writing a query."
    ),
    requires_hitl=False,
    connector="database",
)
async def describe_table(
    table_name: str,
    access_token: str,
    db_type: str = "postgresql",
) -> dict:
    _assert_safe_name(table_name)

    if db_type == "mongodb":
        return await _mongo_describe_collection(table_name, access_token)

    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        import sqlalchemy as sa

        url = _adapt_url(access_token, db_type)
        engine = create_async_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as conn:
                if db_type == "postgresql":
                    result = await conn.execute(sa.text(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :t "
                        "ORDER BY ordinal_position"
                    ), {"t": table_name})
                    columns = [
                        {"name": r[0], "type": r[1], "nullable": r[2] == "YES"}
                        for r in result.fetchall()
                    ]
                else:  # mysql
                    result = await conn.execute(sa.text(f"DESCRIBE `{table_name}`"))
                    columns = [
                        {"name": r[0], "type": r[1], "nullable": r[2] == "YES"}
                        for r in result.fetchall()
                    ]
        finally:
            await engine.dispose()
    except Exception as e:
        logger.error("describe_table failed for %s: %s", table_name, e)
        return {"error": str(e), "table": table_name, "columns": [], "sources": []}

    return {
        "table": table_name,
        "columns": columns,
        "sources": [{"type": "database", "title": f"Schema: {table_name} ({len(columns)} columns)", "url": None}],
    }


@tool(
    description=(
        "Run a MongoDB aggregation pipeline or find query. "
        "Provide collection name and pipeline as a JSON list of stage objects. "
        'Example: [{"$match": {"status": "active"}}, {"$group": {"_id": "$category", "count": {"$sum": 1}}}]'
    ),
    requires_hitl=False,
    connector="database",
)
async def mongodb_query(
    collection: str,
    pipeline: list,
    access_token: str,
    db_type: str = "mongodb",
    limit: int = 100,
) -> dict:
    _assert_safe_name(collection)
    try:
        import motor.motor_asyncio
    except ImportError:
        return {"error": "motor package not installed. Rebuild the Docker image.", "sources": []}

    client = motor.motor_asyncio.AsyncIOMotorClient(access_token, serverSelectionTimeoutMS=5000)
    try:
        db = client.get_default_database()
        full_pipeline = list(pipeline) + [{"$limit": min(limit, 500)}]
        cursor = db[collection].aggregate(full_pipeline)
        docs = []
        async for doc in cursor:
            doc.pop("_id", None)  # ObjectId not JSON-serialisable
            docs.append(doc)
    except Exception as e:
        logger.error("mongodb_query failed for %s: %s", collection, e)
        return {"error": str(e), "collection": collection, "documents": [], "sources": []}
    finally:
        client.close()

    return {
        "documents": docs,
        "count": len(docs),
        "collection": collection,
        "sources": [
            {"type": "database", "title": f"MongoDB {collection} ({len(docs)} document(s))", "url": None}
        ],
    }


# ── MongoDB helpers ───────────────────────────────────────────────────────────

async def _mongo_list_collections(connection_string: str) -> dict:
    try:
        import motor.motor_asyncio
    except ImportError:
        return {"error": "motor package not installed", "tables": [], "sources": []}

    client = motor.motor_asyncio.AsyncIOMotorClient(connection_string, serverSelectionTimeoutMS=5000)
    try:
        db = client.get_default_database()
        collections = await db.list_collection_names()
    except Exception as e:
        return {"error": str(e), "tables": [], "sources": []}
    finally:
        client.close()

    return {
        "tables": collections,
        "count": len(collections),
        "sources": [{"type": "database", "title": f"MongoDB ({len(collections)} collections)", "url": None}],
    }


async def _mongo_describe_collection(collection: str, connection_string: str) -> dict:
    """Sample one document to infer field names and types."""
    try:
        import motor.motor_asyncio
    except ImportError:
        return {"error": "motor package not installed", "table": collection, "columns": [], "sources": []}

    client = motor.motor_asyncio.AsyncIOMotorClient(connection_string, serverSelectionTimeoutMS=5000)
    try:
        db = client.get_default_database()
        sample = await db[collection].find_one({}, {"_id": 0})
    except Exception as e:
        return {"error": str(e), "table": collection, "columns": [], "sources": []}
    finally:
        client.close()

    if not sample:
        return {"table": collection, "columns": [], "note": "Collection is empty", "sources": []}

    columns = [{"name": k, "type": type(v).__name__, "nullable": True} for k, v in sample.items()]
    return {
        "table": collection,
        "columns": columns,
        "note": "Schema inferred from one sample document",
        "sources": [{"type": "database", "title": f"MongoDB schema: {collection}", "url": None}],
    }
