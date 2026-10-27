"""Доступ к Postgres. Схема меняется только миграциями из каталога migrations/."""
import json
import logging
import uuid
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)


async def create_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(database_url, min_size=1, max_size=5)


async def migrate(pool: asyncpg.Pool, directory: Path) -> list[str]:
    """Применяет по порядку непримененные файлы *.sql. Только вперёд, повторно не запускает."""
    applied_now: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version TEXT PRIMARY KEY,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
        )
        done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        for path in sorted(directory.glob("*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.name)
            log.info("Применена миграция %s", path.name)
            applied_now.append(path.name)
    return applied_now


async def schema_ready(pool: asyncpg.Pool) -> bool:
    return await pool.fetchval("SELECT to_regclass('public.sessions') IS NOT NULL")


async def add_event(pool, type_: str, *, chat_id=None, user_id=None, session_id=None, payload=None):
    await pool.execute(
        "INSERT INTO events (type, chat_id, user_id, session_id, payload) VALUES ($1, $2, $3, $4, $5)",
        type_, chat_id, user_id, session_id, json.dumps(payload) if payload is not None else None,
    )


async def add_screened_message(pool, *, chat_id, user_id, username, first_name, message_id, content_type,
                               text, has_link, verdict, decision):
    """Журнал проверок моделью. decision: blocked / passed / unchecked (модель недоступна или выключена)."""
    await pool.execute(
        """INSERT INTO screened_messages (chat_id, user_id, username, first_name, message_id, content_type,
                                          text, has_link, block_probability, category, decision)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
        chat_id, user_id, username, first_name, message_id, content_type, text, has_link,
        verdict.block_probability if verdict else None, verdict.category if verdict else None, decision,
    )


# ---------- участники ----------
async def upsert_member_joined(pool, user_id: int, chat_id: int, username, first_name):
    """Новичок -> pending. Уже подтверждённый при повторном вступлении остаётся verified."""
    await pool.execute(
        """INSERT INTO members (user_id, chat_id, username, first_name, status)
           VALUES ($1, $2, $3, $4, 'pending')
           ON CONFLICT (user_id) DO UPDATE SET
               chat_id = EXCLUDED.chat_id,
               joined_at = CASE WHEN members.status = 'verified' THEN members.joined_at ELSE now() END,
               username = EXCLUDED.username,
               first_name = EXCLUDED.first_name,
               status = CASE WHEN members.status = 'verified' THEN 'verified' ELSE 'pending' END,
               updated_at = now()""",
        user_id, chat_id, username, first_name,
    )


async def mark_member_left(pool, user_id: int):
    await pool.execute(
        "UPDATE members SET status = 'left', updated_at = now() "
        "WHERE user_id = $1 AND status NOT IN ('verified', 'failed')",
        user_id,
    )


async def mark_member_failed(pool, user_id: int):
    await pool.execute(
        "UPDATE members SET status = 'failed', updated_at = now() WHERE user_id = $1", user_id
    )


async def count_failed_attempts(pool, user_id: int) -> int:
    """Истёкшие сессии с момента вступления (при повторном вступлении отсчёт идёт заново)."""
    return await pool.fetchval(
        """SELECT count(*) FROM sessions s JOIN members m ON m.user_id = s.user_id
           WHERE s.user_id = $1 AND s.status = 'expired' AND s.created_at >= m.joined_at""",
        user_id,
    )


async def get_member(pool, user_id: int):
    return await pool.fetchrow("SELECT * FROM members WHERE user_id = $1", user_id)


# ---------- сессии ----------
async def create_session(pool, chat_id: int, user_id: int, trigger_message_id: int, timeout_sec: int):
    """None, если у пользователя уже есть активная сессия."""
    return await pool.fetchrow(
        """INSERT INTO sessions (chat_id, user_id, trigger_message_id, deadline)
           VALUES ($1, $2, $3, now() + make_interval(secs => $4))
           ON CONFLICT (user_id) WHERE status = 'waiting' DO NOTHING
           RETURNING *""",
        chat_id, user_id, trigger_message_id, float(timeout_sec),
    )


async def has_active_session(pool, user_id: int) -> bool:
    return bool(await pool.fetchval(
        "SELECT 1 FROM sessions WHERE user_id = $1 AND status = 'waiting' AND deadline > now()", user_id))


async def set_prompt_message(pool, session_id: uuid.UUID, message_id: int):
    await pool.execute("UPDATE sessions SET prompt_message_id = $2 WHERE id = $1", session_id, message_id)


async def get_session(pool, session_id: uuid.UUID):
    return await pool.fetchrow(
        """SELECT s.*, m.username, m.first_name,
                  EXTRACT(EPOCH FROM (s.deadline - now())) AS seconds_left
           FROM sessions s JOIN members m ON m.user_id = s.user_id
           WHERE s.id = $1""",
        session_id,
    )


async def list_waiting_sessions(pool):
    return await pool.fetch("SELECT id FROM sessions WHERE status = 'waiting'")


async def expire_session(pool, session_id: uuid.UUID):
    """Переводит waiting -> expired. Возвращает строку, только если переход выполнил именно этот вызов."""
    return await pool.fetchrow(
        """UPDATE sessions SET status = 'expired', finished_at = now()
           WHERE id = $1 AND status = 'waiting' RETURNING *""",
        session_id,
    )


# ---------- OAuth ----------
async def create_oauth_state(pool, state: str, session_id: uuid.UUID, user_id: int, ttl_sec: int = 600):
    await pool.execute(
        """INSERT INTO oauth_states (state, session_id, user_id, expires_at)
           VALUES ($1, $2, $3, now() + make_interval(secs => $4))""",
        state, session_id, user_id, float(ttl_sec),
    )


async def consume_oauth_state(pool, state: str):
    """Одноразово: возвращает строку и помечает использованной, иначе None."""
    return await pool.fetchrow(
        """UPDATE oauth_states SET status = 'used'
           WHERE state = $1 AND status = 'new' AND expires_at > now()
           RETURNING session_id, user_id""",
        state,
    )


async def complete_verification(pool, sub_hash: str, session_id: uuid.UUID, user_id: int):
    """Атомарно: сессия waiting и в срок + Google-аккаунт не привязан к другому -> verified.

    Возвращает (результат, строка сессии): 'ok' | 'expired' | 'taken'.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            session = await conn.fetchrow(
                "SELECT *, deadline <= now() AS past_deadline FROM sessions WHERE id = $1 FOR UPDATE",
                session_id,
            )
            if session is None or session["status"] != "waiting" or session["past_deadline"]:
                return "expired", None
            if session["user_id"] != user_id:
                return "expired", None
            owner = await conn.fetchval(
                "SELECT user_id FROM google_identities WHERE google_sub_hash = $1", sub_hash
            )
            if owner is not None and owner != user_id:
                return "taken", None
            if owner is None:
                await conn.execute(
                    "INSERT INTO google_identities (google_sub_hash, user_id) VALUES ($1, $2)",
                    sub_hash, user_id,
                )
            await conn.execute(
                "UPDATE sessions SET status = 'verified', finished_at = now() WHERE id = $1", session_id
            )
            await conn.execute(
                "UPDATE members SET status = 'verified', verified_at = now(), updated_at = now() "
                "WHERE user_id = $1",
                user_id,
            )
            await conn.execute(
                "INSERT INTO events (type, chat_id, user_id, session_id) VALUES ('verified', $1, $2, $3)",
                session["chat_id"], user_id, session_id,
            )
            return "ok", session
