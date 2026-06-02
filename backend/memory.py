"""
Persistent memory layer: sessions, messages, parcel facts, rolling summaries.

Tables created on startup (idempotent):
  users           — anonymous device IDs mapped to stable UUIDs
  parcel_sessions — one session per (user, parcel), carries rolling summary
  messages        — every turn in every session, indexed by session+time
  parcel_memory   — extracted stable facts (proposed params, compliance findings)
  user_memory     — cross-session user preferences (reserved for future auth)

All DB operations are best-effort: failures are logged and the chat still works.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Optional

import asyncpg

logger = logging.getLogger("zoning.memory")

_MEMORY_MODEL    = os.getenv("QUICK_ANSWER_MODEL", "gpt-4.1-mini")
SUMMARIZE_EVERY  = int(os.getenv("SUMMARIZE_EVERY", "15"))

# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA (idempotent — safe to run on every startup)
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    anon_id     TEXT UNIQUE NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    last_seen   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS parcel_sessions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id),
    lat_key          NUMERIC(9,4) NOT NULL,
    lng_key          NUMERIC(9,4) NOT NULL,
    zone_symbol      TEXT,
    exception_number INTEGER,
    title            TEXT,
    message_count    INT DEFAULT 0,
    summary          TEXT,
    summary_at_count INT DEFAULT 0,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, lat_key, lng_key)
);

CREATE TABLE IF NOT EXISTS messages (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    UUID NOT NULL REFERENCES parcel_sessions(id),
    user_id       UUID NOT NULL REFERENCES users(id),
    role          TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content       TEXT NOT NULL,
    token_count   INT,
    endpoint      TEXT,
    sections_used TEXT[],
    zone_symbol   TEXT,
    chunks_count  INT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS message_feedback (
    id          SERIAL PRIMARY KEY,
    message_id  UUID REFERENCES messages(id) ON DELETE CASCADE,
    session_id  UUID NOT NULL,
    user_id     UUID NOT NULL,
    rating      SMALLINT NOT NULL CHECK (rating IN (1, -1)),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feedback_message ON message_feedback(message_id);

CREATE TABLE IF NOT EXISTS parcel_memory (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    UUID NOT NULL REFERENCES parcel_sessions(id),
    user_id       UUID NOT NULL REFERENCES users(id),
    fact_type     TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    confidence    FLOAT DEFAULT 0.8,
    source_msg_id UUID REFERENCES messages(id),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(session_id, key)
);

CREATE TABLE IF NOT EXISTS user_memory (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id),
    category   TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, key)
);
"""


async def ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    logger.info("Memory schema ready")


# ─────────────────────────────────────────────────────────────────────────────
# USER / SESSION RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

async def get_or_create_user(pool: asyncpg.Pool, anon_id: str) -> str:
    """Return stable user UUID for the given anonymous device ID."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO users(anon_id) VALUES($1) "
            "ON CONFLICT(anon_id) DO UPDATE SET last_seen=NOW() "
            "RETURNING id",
            anon_id[:200],
        )
        return str(row["id"])


async def get_or_create_session(
    pool: asyncpg.Pool,
    user_id: str,
    lat: float,
    lng: float,
    zone_symbol: Optional[str] = None,
    exception_number: Optional[int] = None,
) -> str:
    """Return session UUID for this (user, parcel) pair, creating if needed."""
    lat_key = round(lat, 4)
    lng_key = round(lng, 4)
    title = f"{lat_key} / {lng_key}{f' — {zone_symbol}' if zone_symbol else ''}"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO parcel_sessions(user_id, lat_key, lng_key, zone_symbol, exception_number, title)
            VALUES($1, $2, $3, $4, $5, $6)
            ON CONFLICT(user_id, lat_key, lng_key) DO UPDATE SET
                zone_symbol      = COALESCE(EXCLUDED.zone_symbol, parcel_sessions.zone_symbol),
                exception_number = COALESCE(EXCLUDED.exception_number, parcel_sessions.exception_number),
                updated_at       = NOW()
            RETURNING id
            """,
            user_id, lat_key, lng_key, zone_symbol, exception_number, title,
        )
        return str(row["id"])


async def get_session_info(
    pool: asyncpg.Pool,
    user_id: str,
    lat: float,
    lng: float,
) -> Optional[dict]:
    """Return session metadata for /api/sessions endpoint. None if no prior messages."""
    lat_key = round(lat, 4)
    lng_key = round(lng, 4)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, title, message_count, summary, updated_at "
            "FROM parcel_sessions "
            "WHERE user_id=$1 AND lat_key=$2 AND lng_key=$3",
            user_id, lat_key, lng_key,
        )
        if not row or row["message_count"] == 0:
            return None
        params = await conn.fetch(
            "SELECT key, value, fact_type FROM parcel_memory "
            "WHERE session_id=$1 ORDER BY updated_at DESC",
            row["id"],
        )
        return {
            "session_id":    str(row["id"]),
            "message_count": row["message_count"],
            "summary":       row["summary"],
            "updated_at":    row["updated_at"].isoformat() if row["updated_at"] else None,
            "params":        [
                {"key": p["key"], "value": p["value"], "type": p["fact_type"]}
                for p in params
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE PERSISTENCE
# ─────────────────────────────────────────────────────────────────────────────

async def load_messages(pool: asyncpg.Pool, session_id: str, limit: int = 15) -> list[dict]:
    """Load recent messages for LLM context. Returns oldest-first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content FROM (
                SELECT role, content, created_at FROM messages
                WHERE session_id=$1
                ORDER BY created_at DESC LIMIT $2
            ) sub ORDER BY created_at ASC
            """,
            session_id, limit,
        )
        return [{"role": r["role"], "content": r["content"]} for r in rows]


async def load_messages_for_display(
    pool: asyncpg.Pool,
    session_id: str,
    limit: int = 20,
) -> list[dict]:
    """Load messages for frontend display (role + content, oldest-first)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content FROM (
                SELECT role, content, created_at FROM messages
                WHERE session_id=$1
                ORDER BY created_at DESC LIMIT $2
            ) sub ORDER BY created_at ASC
            """,
            session_id, limit,
        )
        return [{"role": r["role"], "content": r["content"], "mode": "full"} for r in rows]


async def save_message_pair(
    pool: asyncpg.Pool,
    session_id: str,
    user_id: str,
    user_msg: str,
    assistant_msg: str,
    sections_used: Optional[list[str]] = None,
    zone_symbol: Optional[str] = None,
    chunks_count: Optional[int] = None,
) -> str:
    """
    Persist user+assistant turn atomically.
    Returns the assistant message's UUID (used as source_msg_id for fact extraction).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO messages(session_id, user_id, role, content, endpoint) "
                "VALUES($1, $2, 'user', $3, 'chat')",
                session_id, user_id, user_msg,
            )
            asst_row = await conn.fetchrow(
                "INSERT INTO messages(session_id, user_id, role, content, endpoint, "
                "sections_used, zone_symbol, chunks_count) "
                "VALUES($1, $2, 'assistant', $3, 'chat', $4, $5, $6) RETURNING id",
                session_id, user_id, assistant_msg,
                sections_used or [], zone_symbol, chunks_count,
            )
            await conn.execute(
                "UPDATE parcel_sessions "
                "SET message_count = message_count + 2, updated_at = NOW() "
                "WHERE id=$1",
                session_id,
            )
            return str(asst_row["id"])


async def get_parcel_params(pool: asyncpg.Pool, session_id: str) -> list[dict]:
    """Return confirmed parcel parameters extracted from this session."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, value, fact_type FROM parcel_memory "
            "WHERE session_id=$1 ORDER BY updated_at DESC",
            session_id,
        )
        return [{"key": r["key"], "value": r["value"], "type": r["fact_type"]} for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# PARCEL FACT EXTRACTION (background, lightweight)
# ─────────────────────────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """\
You are a data extractor for a Toronto zoning consultation tool.
From the exchange below, extract ONLY stable confirmed facts.

Extract ONLY when the USER confirms or states a value — not AI explanations:
- Proposed building parameters (units, height_m, gfa_m2, coverage_pct, setbacks, depth)
- Compliance findings established (compliant/non-compliant + brief reason)
- Lot details the user confirmed from a survey
- Decisions the user committed to

IGNORE: questions, hypotheticals ("what if", "could I"), bylaw values from the AI.

Return JSON only — no markdown, no explanation:
{"facts": [{"key": "proposed_units", "value": "4", "type": "proposed_param"}]}
Return {"facts": []} if nothing qualifies.

Fact types: proposed_param | compliance_finding | confirmed_detail | user_decision"""

_TRIGGER_RE = re.compile(
    r'\b(\d+\.?\d*\s*(m|m²|m2|units?|floors?|%|storeys?|parking)?'
    r'|want|plan|decided|confirmed|going to|will have|survey|measured)\b',
    re.IGNORECASE,
)


async def extract_and_save_facts(
    pool: asyncpg.Pool,
    openai_client,
    session_id: str,
    user_id: str,
    user_msg: str,
    assistant_msg: str,
    source_msg_id: str,
) -> None:
    """Background: extract confirmed facts and upsert into parcel_memory."""
    if not _TRIGGER_RE.search(user_msg):
        return

    try:
        exchange = f"USER: {user_msg}\n\nASSISTANT: {assistant_msg[:800]}"
        resp = await openai_client.chat.completions.create(
            model=_MEMORY_MODEL,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user",   "content": exchange},
            ],
            max_tokens=400,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw   = resp.choices[0].message.content or '{"facts": []}'
        facts = json.loads(raw).get("facts", [])
        if not facts:
            return

        async with pool.acquire() as conn:
            for fact in facts[:10]:
                key   = str(fact.get("key",  "")).strip()[:100]
                value = str(fact.get("value","")).strip()[:500]
                ftype = str(fact.get("type", "proposed_param")).strip()[:50]
                if not key or not value:
                    continue
                await conn.execute(
                    """
                    INSERT INTO parcel_memory(session_id, user_id, fact_type, key, value, source_msg_id)
                    VALUES($1, $2, $3, $4, $5, $6)
                    ON CONFLICT(session_id, key) DO UPDATE SET
                        value         = EXCLUDED.value,
                        fact_type     = EXCLUDED.fact_type,
                        source_msg_id = EXCLUDED.source_msg_id,
                        updated_at    = NOW()
                    """,
                    session_id, user_id, ftype, key, value, source_msg_id,
                )
        logger.info("[memory] extracted %d facts for session %.8s", len(facts), session_id)
    except Exception as exc:
        logger.warning("[memory] extract_and_save_facts: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# ROLLING SUMMARY (background, fires every SUMMARIZE_EVERY new messages)
# ─────────────────────────────────────────────────────────────────────────────

_SUMMARY_SYSTEM = """\
Summarize this Toronto zoning consultation. Preserve ALL of:

PARCEL: Zone symbol, exception number, lot dimensions, active overlays
PROPOSED DEVELOPMENT: Every building parameter discussed (height, units, GFA,
  coverage, setbacks, parking, bicycle)
COMPLIANCE STATUS: What is compliant, what is not — with the specific section
  number and limit
KEY DECISIONS: What the user confirmed or decided
UNRESOLVED QUESTIONS: What has not been answered

Format as structured bullet points, not prose. Include all numbers with units.
Keep under 500 words. Never drop a compliance finding or confirmed measurement."""


async def maybe_summarize(pool: asyncpg.Pool, openai_client, session_id: str) -> None:
    """Background: generate a rolling summary if SUMMARIZE_EVERY new messages have accrued."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT message_count, summary_at_count "
                "FROM parcel_sessions WHERE id=$1",
                session_id,
            )
            if not row:
                return
            if row["message_count"] - row["summary_at_count"] < SUMMARIZE_EVERY:
                return

            msgs = await conn.fetch(
                "SELECT role, content FROM messages "
                "WHERE session_id=$1 ORDER BY created_at ASC",
                session_id,
            )

        conv = "\n".join(
            f"{r['role'].upper()}: {r['content'][:600]}" for r in msgs
        )
        resp = await openai_client.chat.completions.create(
            model=_MEMORY_MODEL,
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user",   "content": f"Conversation:\n\n{conv}"},
            ],
            max_tokens=600,
            temperature=0.1,
        )
        summary = resp.choices[0].message.content or ""

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE parcel_sessions "
                "SET summary=$1, summary_at_count=message_count WHERE id=$2",
                summary, session_id,
            )
        logger.info("[memory] summary generated for session %.8s", session_id)
    except Exception as exc:
        logger.warning("[memory] maybe_summarize: %s", exc)
