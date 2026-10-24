import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from aiogram.types import ChatMemberLeft, ChatMemberMember

from app import bot as bot_handlers
from app import db, google, service, texts
from app.web import create_app
from tests.conftest import CHAT_ID, FakeMessage, join_update, make_user, member_update

MIGRATIONS = Path(__file__).parent.parent / "migrations"


async def join(ctx, user_id=1, username="nick"):
    await bot_handlers.on_chat_member(join_update(user_id, username), ctx)


async def first_message(ctx, user_id=1, message_id=10, username="nick"):
    msg = FakeMessage(make_user(user_id, username), message_id)
    await bot_handlers.on_group_message(msg, ctx)
    return msg


async def test_migrate_is_idempotent(pool):
    assert await db.migrate(pool, MIGRATIONS) == []


async def test_status_of_handles_enum():
    assert bot_handlers.status_of(ChatMemberLeft(user=make_user_pyd())) == "left"


def make_user_pyd():
    from aiogram.types import User
    return User(id=1, is_bot=False, first_name="N")


async def test_join_records_pending_member(ctx):
    await join(ctx)
    m = await db.get_member(ctx.pool, 1)
    assert m["status"] == "pending" and m["username"] == "nick"


async def test_bots_and_unknown_users_are_ignored(ctx):
    await first_message(ctx, user_id=77)  # не вступал после бота
    assert not ctx.bot.of("send")
    await bot_handlers.on_chat_member(
        member_update(ChatMemberLeft, ChatMemberMember, user_id=5), ctx)
    # бот
    await bot_handlers.on_chat_member(join_update(9, is_bot=True), ctx)
    assert await db.get_member(ctx.pool, 9) is None


async def test_first_message_gets_reply_with_mention_and_link(ctx):
    await join(ctx)
    msg = await first_message(ctx, message_id=10)
    sends = ctx.bot.of("send")
    assert len(sends) == 1
    _, chat_id, text, kw = sends[0]
    assert chat_id == CHAT_ID and "@nick" in text and "2 сек." in text
    assert kw["reply_parameters"].message_id == 10  # реплай на сообщение новичка
    session = await ctx.pool.fetchrow("SELECT * FROM sessions")
    assert session["status"] == "waiting" and session["trigger_message_id"] == 10
    assert session["prompt_message_id"] == 1001
    url = kw["reply_markup"].inline_keyboard[0][0].url
    assert url == f"https://t.me/testbot?start={session['id']}"
    assert not msg.deleted  # первое сообщение остаётся
    assert str(session["id"]) in ctx.timers


async def test_further_messages_deleted_while_session_active(ctx):
    await join(ctx)
    await first_message(ctx, message_id=10)
    second = await first_message(ctx, message_id=11)
    assert second.deleted
    assert len(ctx.bot.of("send")) == 1  # второго запроса нет


async def test_timeout_deletes_both_messages_and_keeps_member_pending(ctx):
    await join(ctx)
    await first_message(ctx)
    await asyncio.sleep(3.6)
    session = await ctx.pool.fetchrow("SELECT * FROM sessions")
    assert session["status"] == "expired"
    assert ("delete", CHAT_ID, 1001) in ctx.bot.of("delete")  # сообщение бота
    assert ("delete", CHAT_ID, 10) in ctx.bot.of("delete")  # сообщение новичка
    assert not ctx.bot.of("ban")  # одна неудача ещё не блокировка
    assert (await db.get_member(ctx.pool, 1))["status"] == "pending"
    edits = [c[1]["text"] for c in ctx.bot.of("edit")]
    assert any("1 сек." in t for t in edits)  # счётчик действительно обновлялся
    # после таймаута следующее сообщение запускает новую сессию
    await first_message(ctx, message_id=20)
    assert await ctx.pool.fetchval("SELECT count(*) FROM sessions") == 2


async def test_verification_edits_message_and_deletes_it_later(ctx, monkeypatch):
    monkeypatch.setattr(service, "SUCCESS_DELETE_DELAY_SEC", 0.05)
    await join(ctx)
    await first_message(ctx)
    session = await ctx.pool.fetchrow("SELECT * FROM sessions")
    result, row = await db.complete_verification(ctx.pool, "hash1", session["id"], 1)
    assert result == "ok"
    await service.finish_verified(ctx, row)
    await asyncio.sleep(0.3)
    last_edit = ctx.bot.of("edit")[-1][1]
    assert last_edit["text"] == texts.SUCCESS_TEXT and last_edit["reply_markup"] is None
    assert ("delete", CHAT_ID, 1001) in ctx.bot.of("delete")
    assert not any(c[1] == 10 for c in ctx.bot.of("delete"))  # сообщение пользователя не тронуто
    assert (await db.get_member(ctx.pool, 1))["status"] == "verified"
    assert str(session["id"]) not in ctx.timers
    # после верификации новые сообщения игнорируются
    await first_message(ctx, message_id=30)
    assert len(ctx.bot.of("send")) == 1


async def test_google_account_cannot_verify_two_members(ctx):
    for uid in (1, 2):
        await join(ctx, uid, f"u{uid}")
        await first_message(ctx, uid, 10 * uid, f"u{uid}")
    s1, s2 = await ctx.pool.fetch("SELECT * FROM sessions ORDER BY user_id")
    assert (await db.complete_verification(ctx.pool, "same", s1["id"], 1))[0] == "ok"
    result, _ = await db.complete_verification(ctx.pool, "same", s2["id"], 2)
    assert result == "taken"
    assert (await db.get_member(ctx.pool, 2))["status"] == "pending"
    assert (await ctx.pool.fetchrow("SELECT status FROM sessions WHERE user_id=2"))["status"] == "waiting"


async def test_cannot_verify_after_deadline(ctx):
    await join(ctx)
    await first_message(ctx)
    session = await ctx.pool.fetchrow("SELECT * FROM sessions")
    await ctx.pool.execute("UPDATE sessions SET deadline = now() - interval '1 second'")
    result, _ = await db.complete_verification(ctx.pool, "h", session["id"], 1)
    assert result == "expired"
    assert await ctx.pool.fetchval("SELECT count(*) FROM google_identities") == 0


async def test_start_in_dm_issues_unique_link_only_to_owner(ctx):
    await join(ctx)
    await first_message(ctx)
    session = await ctx.pool.fetchrow("SELECT * FROM sessions")

    stranger = FakeMessage(make_user(2), 1, chat_id=2)
    await bot_handlers.on_start(stranger, SimpleNamespace(args=str(session["id"])), ctx)
    assert "недействительна" in stranger.answers[0][0]
    assert await ctx.pool.fetchval("SELECT count(*) FROM oauth_states") == 0

    owner = FakeMessage(make_user(1), 1, chat_id=1)
    await bot_handlers.on_start(owner, SimpleNamespace(args=str(session["id"])), ctx)
    url = owner.answers[0][1]["reply_markup"].inline_keyboard[0][0].url
    state = await ctx.pool.fetchval("SELECT state FROM oauth_states")
    assert url.startswith("https://accounts.google.com/") and f"state={state}" in url

    garbage = FakeMessage(make_user(1), 1, chat_id=1)
    await bot_handlers.on_start(garbage, SimpleNamespace(args="not-a-uuid"), ctx)
    assert "Авторизоваться" in garbage.answers[0][0]


async def test_oauth_state_is_single_use(ctx):
    await join(ctx)
    await first_message(ctx)
    session = await ctx.pool.fetchrow("SELECT * FROM sessions")
    await db.create_oauth_state(ctx.pool, "S1", session["id"], 1)
    assert await db.consume_oauth_state(ctx.pool, "S1") is not None
    assert await db.consume_oauth_state(ctx.pool, "S1") is None
    await db.create_oauth_state(ctx.pool, "S2", session["id"], 1, ttl_sec=-5)
    assert await db.consume_oauth_state(ctx.pool, "S2") is None  # просрочен


async def _callback_client(ctx, monkeypatch, identity):
    async def fake_identity(settings, code):
        return identity

    monkeypatch.setattr(google, "fetch_identity", fake_identity)
    web_app = create_app()
    web_app.state.ctx = ctx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=web_app), base_url="http://t")


def _dms(ctx, user_id):
    return [c[2] for c in ctx.bot.of("send") if c[1] == user_id]


async def test_google_callback_end_to_end(ctx, monkeypatch):
    monkeypatch.setattr(service, "SUCCESS_DELETE_DELAY_SEC", 0.05)
    await join(ctx)
    await first_message(ctx)
    session = await ctx.pool.fetchrow("SELECT * FROM sessions")
    await db.create_oauth_state(ctx.pool, "STATE", session["id"], 1)

    async with await _callback_client(ctx, monkeypatch, {"sub": "g-123", "email_verified": True}) as client:
        bad = await client.get("/auth/google/callback", params={"code": "c", "state": "NOPE"})
        assert bad.status_code == 400
        ok = await client.get("/auth/google/callback", params={"code": "c", "state": "STATE"})
        # сразу назад в бота, без отдельной страницы
        assert ok.status_code == 302 and ok.headers["location"] == "https://t.me/testbot"
        replay = await client.get("/auth/google/callback", params={"code": "c", "state": "STATE"})
        assert replay.status_code == 400  # state одноразовый
        cancelled = await client.get("/auth/google/callback", params={"error": "access_denied"})
        assert cancelled.status_code == 400
    await asyncio.sleep(0.3)
    assert (await db.get_member(ctx.pool, 1))["status"] == "verified"
    assert ctx.bot.of("edit")[-1][1]["text"] == texts.SUCCESS_TEXT  # сообщение в группе
    assert _dms(ctx, 1) == ["✅ Вы верифицированы! Теперь можно писать в группе."]  # статус в личке
    raw = await ctx.pool.fetchval("SELECT google_sub_hash FROM google_identities")
    assert "g-123" not in raw


async def test_callback_reports_problems_in_dm_and_redirects(ctx, monkeypatch):
    for uid in (1, 2):
        await join(ctx, uid, f"u{uid}")
        await first_message(ctx, uid, 10 * uid, f"u{uid}")
    s1, s2 = await ctx.pool.fetch("SELECT * FROM sessions ORDER BY user_id")
    await db.create_oauth_state(ctx.pool, "A", s1["id"], 1)
    await db.create_oauth_state(ctx.pool, "B", s2["id"], 2)
    await db.create_oauth_state(ctx.pool, "C", s2["id"], 2)

    same = {"sub": "same", "email_verified": True}
    async with await _callback_client(ctx, monkeypatch, same) as client:
        assert (await client.get("/auth/google/callback", params={"code": "c", "state": "A"})).status_code == 302
        r = await client.get("/auth/google/callback", params={"code": "c", "state": "B"})
        assert r.status_code == 302 and "уже привязан" in _dms(ctx, 2)[-1]  # чужой Google

    async with await _callback_client(ctx, monkeypatch, {"sub": "x", "email_verified": False}) as client:
        await client.get("/auth/google/callback", params={"code": "c", "state": "C"})
        assert "не подтверждён" in _dms(ctx, 2)[-1]

    await ctx.pool.execute("UPDATE sessions SET deadline = now() - interval '1 second' WHERE user_id = 2")
    await db.create_oauth_state(ctx.pool, "D", s2["id"], 2)
    async with await _callback_client(ctx, monkeypatch, {"sub": "y", "email_verified": True}) as client:
        await client.get("/auth/google/callback", params={"code": "c", "state": "D"})
        assert "Время вышло" in _dms(ctx, 2)[-1]

    await db.create_oauth_state(ctx.pool, "E", s2["id"], 2)
    async with await _callback_client(ctx, monkeypatch, None) as client:  # Google отказал
        await client.get("/auth/google/callback", params={"code": "c", "state": "E"})
        assert "не подтвердил вход" in _dms(ctx, 2)[-1]


async def test_recover_closes_overdue_sessions(ctx):
    await join(ctx)
    await first_message(ctx)
    for t in ctx.timers.values():
        t.cancel()
    ctx.timers.clear()
    await ctx.pool.execute("UPDATE sessions SET deadline = now() - interval '5 seconds'")
    await service.recover(ctx)
    await asyncio.sleep(0.3)
    assert (await ctx.pool.fetchrow("SELECT status FROM sessions"))["status"] == "expired"
    assert ("delete", CHAT_ID, 1001) in ctx.bot.of("delete")


async def _fail_one_cycle(ctx, message_id):
    """Новичок пишет, время сессии выходит."""
    await first_message(ctx, message_id=message_id)
    session = await ctx.pool.fetchrow("SELECT * FROM sessions WHERE status = 'waiting'")
    for t in list(ctx.timers.values()):
        t.cancel()
    ctx.timers.clear()
    await ctx.pool.execute(
        "UPDATE sessions SET deadline = now() - interval '1 second' WHERE id = $1", session["id"])
    await service.expire(ctx, session["id"])


async def test_three_failed_attempts_ban_the_user(ctx):
    await join(ctx)
    await _fail_one_cycle(ctx, 10)
    await _fail_one_cycle(ctx, 11)
    assert not ctx.bot.of("ban")
    await _fail_one_cycle(ctx, 12)
    assert ctx.bot.of("ban") == [("ban", CHAT_ID, 1)]
    assert (await db.get_member(ctx.pool, 1))["status"] == "failed"
    assert await ctx.pool.fetchval("SELECT count(*) FROM events WHERE type = 'banned'") == 1
    # каждое сообщение из трёх циклов было удалено вместе с сообщением бота
    deleted = {c[2] for c in ctx.bot.of("delete")}
    assert {10, 11, 12} <= deleted


async def test_ban_event_does_not_overwrite_failed_and_rejoin_resets_attempts(ctx):
    import datetime
    from aiogram.types import ChatMemberBanned
    await join(ctx)
    for n in range(3):
        await _fail_one_cycle(ctx, 10 + n)
    await bot_handlers.on_chat_member(
        member_update(ChatMemberMember, lambda user: ChatMemberBanned(
            user=user, until_date=datetime.datetime.now())), ctx)
    assert (await db.get_member(ctx.pool, 1))["status"] == "failed"  # не затёрт статусом left
    # админ разбанил, человек вступил снова: попытки считаются заново
    await join(ctx)
    assert (await db.get_member(ctx.pool, 1))["status"] == "pending"
    assert await db.count_failed_attempts(ctx.pool, 1) == 0
    await _fail_one_cycle(ctx, 40)
    assert len(ctx.bot.of("ban")) == 1  # повторной блокировки после первой неудачи нет


async def test_verified_user_is_never_banned(ctx):
    await join(ctx)
    await _fail_one_cycle(ctx, 10)
    await _fail_one_cycle(ctx, 11)
    await first_message(ctx, message_id=12)
    session = await ctx.pool.fetchrow("SELECT * FROM sessions WHERE status = 'waiting'")
    result, _ = await db.complete_verification(ctx.pool, "hv", session["id"], 1)
    assert result == "ok"
    assert not ctx.bot.of("ban") and (await db.get_member(ctx.pool, 1))["status"] == "verified"


# ---------- какие сообщения вообще проверяются ----------
def _group_message(**fields):
    import datetime
    from aiogram.types import Chat, Message
    return Message(message_id=1, date=datetime.datetime.now(),
                   chat=Chat(id=CHAT_ID, type="supergroup"), **fields)


def test_unusual_content_types_are_checked_like_any_message():
    from aiogram.types import Chat, Dice, Story
    unusual = [
        {"text": "привет"},
        {"dice": Dice(emoji="🎲", value=3)},
        {"story": Story(chat=Chat(id=-1, type="channel"), id=5)},  # пересланная история
        {},  # тип, который aiogram не знает (UNKNOWN): безопаснее проверить
    ]
    for fields in unusual:
        assert bot_handlers.USER_MESSAGE.resolve(_group_message(**fields)), fields


def test_service_messages_are_not_user_messages():
    from aiogram.types import User
    u = User(id=1, is_bot=False, first_name="N")
    assert not bot_handlers.USER_MESSAGE.resolve(_group_message(new_chat_members=[u]))
    assert not bot_handlers.USER_MESSAGE.resolve(_group_message(left_chat_member=u))


async def test_dispatcher_checks_dice_from_pending_user_but_not_join_notice(ctx):
    """Сквозная проверка маршрутизации: фильтры настоящего диспетчера, без подмены обработчиков."""
    from aiogram import Bot, Dispatcher
    from aiogram.types import Dice, Update, User

    await join(ctx)
    u = User(id=1, is_bot=False, first_name="Nick", username="nick")
    dp = Dispatcher(ctx=ctx)
    dp.include_router(bot_handlers.router)
    try:
        bot = Bot("123:abc")
        # служебное «X вступил в группу» не должно запускать проверку
        await dp.feed_update(bot, Update(update_id=1, message=_group_message(
            from_user=u, new_chat_members=[u])))
        assert not ctx.bot.of("send")
        # кубик от новичка проверяется, как обычное сообщение
        await dp.feed_update(bot, Update(update_id=2, message=_group_message(
            from_user=u, dice=Dice(emoji="🎲", value=3))))
        assert len(ctx.bot.of("send")) == 1
        await bot.session.close()
    finally:
        bot_handlers.router._parent_router = None
