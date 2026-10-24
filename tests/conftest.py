import asyncio
import datetime
import os
import pathlib
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from aiogram.types import Chat, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, User

from app import db
from app.config import Settings
from app.context import Ctx

MIGRATIONS = pathlib.Path(__file__).parent.parent / "migrations"
DSN = os.environ.get("TEST_DATABASE_URL", "")
CHAT_ID = -100500


class FakeBot:
    """Записывает вызовы Telegram API вместо реальных запросов."""

    def __init__(self):
        self.calls = []
        self.ban_kwargs = []
        self._next_id = 1000

    async def send_message(self, chat_id, text, **kw):
        self._next_id += 1
        self.calls.append(("send", chat_id, text, kw))
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, **kw):
        self.calls.append(("edit", kw))

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete", chat_id, message_id))

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.calls.append(("ban", chat_id, user_id))
        self.ban_kwargs.append(kw)

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


class FakeMessage:
    def __init__(self, user, message_id, chat_id=CHAT_ID, text=None, caption=None, entities=None):
        self.from_user = user
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id)
        self.text = text
        self.caption = caption
        self.entities = entities
        self.caption_entities = None
        self.sender_chat = None
        self.deleted = False
        self.answers = []

    async def delete(self):
        self.deleted = True

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


def make_user(user_id=1, username="nick", first_name="Nick", is_bot=False):
    return SimpleNamespace(id=user_id, username=username, first_name=first_name,
                           full_name=first_name, is_bot=is_bot)


def member_update(old_cls, new_cls, user_id=1, username="nick", is_bot=False):
    u = User(id=user_id, is_bot=is_bot, first_name="Nick", username=username)
    return ChatMemberUpdated(
        chat=Chat(id=CHAT_ID, type="supergroup"), from_user=u,
        date=datetime.datetime.now(), old_chat_member=old_cls(user=u), new_chat_member=new_cls(user=u),
    )


def join_update(user_id=1, username="nick", is_bot=False):
    return member_update(ChatMemberLeft, ChatMemberMember, user_id, username, is_bot)


@pytest_asyncio.fixture
async def pool():
    if not DSN:
        pytest.skip("TEST_DATABASE_URL не задан")
    # Тесты сбрасывают схему: только локальная одноразовая база
    if not any(h in DSN for h in ("127.0.0.1", "localhost")):
        pytest.fail("TEST_DATABASE_URL должен указывать на локальную базу")
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with p.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await db.migrate(p, MIGRATIONS)
    yield p
    await p.close()


@pytest.fixture
def settings():
    return Settings(
        bot_token="x", chat_id=CHAT_ID, database_url=DSN, google_client_id="cid.apps.test",
        google_client_secret="sec", public_base_url="https://app.test", secret_key="k" * 32,
        port=8000, verify_timeout_sec=2, max_failed_attempts=3, run_migrations=False,
    )


@pytest_asyncio.fixture
async def ctx(pool, settings):
    c = Ctx(settings=settings, pool=pool, bot=FakeBot(), bot_username="testbot")
    yield c
    for t in list(c.timers.values()) + list(c.background):
        t.cancel()
    await asyncio.sleep(0)
