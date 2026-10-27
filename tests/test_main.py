"""Запуск приложения целиком (lifespan) с подменой Telegram-бота."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

import main
from app.bot import router as bot_router
from tests.conftest import DSN


class StartupBot:
    def __init__(self, token):
        self.session = SimpleNamespace(close=self._close)
        self.closed = False

    async def _close(self):
        self.closed = True

    async def get_me(self):
        return SimpleNamespace(username="testbot")


@pytest.fixture
def env(monkeypatch, pool):
    for key, value in {
        "BOT_TOKEN": "123:fake", "CHAT_ID": "-100500", "DATABASE_URL": DSN,
        "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "sec",
        "PUBLIC_BASE_URL": "https://app.test", "SECRET_KEY": "k" * 32,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RUN_MIGRATIONS", raising=False)  # по умолчанию миграции включены

    async def forever(self, *args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "Bot", StartupBot)
    monkeypatch.setattr(main.Dispatcher, "start_polling", forever)
    yield
    # в продакшене Dispatcher один; в тестах роутер подключается к новому при каждом запуске
    bot_router._parent_router = None


async def test_main_app_starts_serves_health_and_stops(env):
    async with main.lifespan(main.app):
        assert main.app.state.ctx.bot_username == "testbot"
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert (await client.get("/health")).json() == {"ok": True}


async def test_start_fails_clearly_when_migrations_disabled_and_schema_missing(env, pool, monkeypatch):
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    monkeypatch.setenv("RUN_MIGRATIONS", "0")
    with pytest.raises(RuntimeError, match="RUN_MIGRATIONS=0"):
        async with main.lifespan(main.app):
            pass


async def test_empty_database_gets_all_tables_by_default(env, pool):
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    async with main.lifespan(main.app):
        pass
    tables = {r["tablename"] for r in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert {"members", "sessions", "oauth_states", "google_identities", "events", "screened_messages"} <= tables
    assert await pool.fetchval("SELECT count(*) FROM schema_migrations") == 2
    bot_router._parent_router = None
    async with main.lifespan(main.app):  # повторный старт безопасен
        pass
    assert await pool.fetchval("SELECT count(*) FROM schema_migrations") == 2
