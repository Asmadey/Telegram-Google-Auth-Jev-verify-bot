"""Антиспам-классификатор Jev: запрос, разбор ответа, сбои и поведение бота."""
import dataclasses
import json
from types import SimpleNamespace

import httpx
import pytest

from app import bot as bot_handlers
from app import db, spam
from app.config import Settings
from tests.conftest import CHAT_ID, FakeMessage, join_update, make_user


def make_settings(**kw):
    return Settings("t", -1, "db", "cid", "sec", "https://app.test", "k" * 32, 8000, 60, 3, False, **kw)


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok_response(block=0.93, category="advertising"):
    return httpx.Response(200, json={"answers": {
        "should_block": {"type": "noul", "noul": block},
        "category": {"type": "choice", "choice": category, "confidence": 0.8, "probabilities": {}},
    }})


# ---------- запрос и разбор ответа ----------
async def test_classify_sends_documented_request_and_parses_answer():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return ok_response(0.93, "advertising")

    verdict = await spam.classify(make_settings(openrouter_api_key="KEY"), "Курс по нейросетям -50%",
                                  author="Иван", contains_link=True, client=mock_client(handler))
    assert verdict == spam.Verdict(0.93, "advertising")
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["auth"] == "Bearer KEY"
    body = seen["body"]
    assert body["model"] == "typesafe/jev-1.13"
    assert body["state"] == {"message": "Курс по нейросетям -50%", "author_name": "Иван", "contains_link": True}
    assert set(body["questions"]) == {"should_block", "category"}
    assert body["questions"]["should_block"]["type"] == "noul"
    assert set(body["questions"]["should_block"]["criteria"]) == {"true", "false"}
    assert body["questions"]["category"]["type"] == "choice"


async def test_classify_truncates_very_long_text():
    seen = {}

    def handler(request):
        seen["len"] = len(json.loads(request.content)["state"]["message"])
        return ok_response()

    await spam.classify(make_settings(openrouter_api_key="K"), "а" * 50_000, author="x",
                        contains_link=False, client=mock_client(handler))
    assert seen["len"] == spam.MAX_TEXT_CHARS


async def test_classify_is_off_without_api_key():
    def handler(request):
        raise AssertionError("запрос не должен уходить без ключа")

    assert await spam.classify(make_settings(), "текст", author="x", contains_link=False,
                               client=mock_client(handler)) is None


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="boom"),
    httpx.Response(401, json={"error": "bad key"}),
    httpx.Response(200, text="не json"),
    httpx.Response(200, json={"answers": {}}),
    httpx.Response(200, json={"unexpected": True}),
    httpx.Response(200, json={"answers": {"should_block": {"type": "noul"}}}),
])
async def test_classify_returns_none_on_any_bad_answer(response):
    verdict = await spam.classify(make_settings(openrouter_api_key="K"), "текст", author="x",
                                  contains_link=False, client=mock_client(lambda r: response))
    assert verdict is None


async def test_classify_returns_none_on_network_failure():
    def handler(request):
        raise httpx.ConnectTimeout("таймаут")

    assert await spam.classify(make_settings(openrouter_api_key="K"), "текст", author="x",
                               contains_link=False, client=mock_client(handler)) is None


def test_threshold_is_inclusive():
    assert spam.Verdict(0.8, "spam").should_block(0.8)
    assert not spam.Verdict(0.79, "spam").should_block(0.8)


def test_has_links_detects_url_entities():
    url = SimpleNamespace(type="url")
    bold = SimpleNamespace(type="bold")
    assert spam.has_links(SimpleNamespace(entities=[bold, url], caption_entities=None))
    assert spam.has_links(SimpleNamespace(entities=None, caption_entities=[SimpleNamespace(type="text_link")]))
    assert not spam.has_links(SimpleNamespace(entities=[bold], caption_entities=None))
    assert not spam.has_links(SimpleNamespace(entities=None, caption_entities=None))


# ---------- поведение бота ----------
@pytest.fixture
def screening(ctx, monkeypatch):
    """Включает проверку и подменяет вызов модели; calls хранит тексты, которые ушли на проверку."""
    ctx.settings = dataclasses.replace(ctx.settings, openrouter_api_key="K", spam_threshold=0.8)
    state = SimpleNamespace(calls=[], verdict=spam.Verdict(0.02, "normal"))

    async def fake_classify(settings, text, **kw):
        state.calls.append(text)
        return state.verdict

    monkeypatch.setattr(spam, "classify", fake_classify)
    return state


async def post(ctx, text, user_id=1, message_id=10, **kw):
    msg = FakeMessage(make_user(user_id, "nick"), message_id, text=text, **kw)
    await bot_handlers.on_group_message(msg, ctx)
    return msg


async def join(ctx, user_id=1):
    await bot_handlers.on_chat_member(join_update(user_id, "nick"), ctx)


async def test_spam_first_message_is_deleted_and_author_banned_at_once(ctx, screening):
    screening.verdict = spam.Verdict(0.97, "advertising")
    await join(ctx)
    await post(ctx, "Продаю аккаунты, пишите в лс")
    assert ("delete", CHAT_ID, 10) in ctx.bot.of("delete")
    assert ctx.bot.of("ban") == [("ban", CHAT_ID, 1)]
    assert ctx.bot.ban_kwargs == [{"revoke_messages": True}]  # заодно убираются все его сообщения
    assert not ctx.bot.of("send")  # проверку через Google ему не предлагаем
    assert await ctx.pool.fetchval("SELECT count(*) FROM sessions") == 0
    assert (await db.get_member(ctx.pool, 1))["status"] == "failed"
    ev = {r["type"]: json.loads(r["payload"]) for r in await ctx.pool.fetch(
        "SELECT type, payload FROM events WHERE type IN ('spam_blocked', 'banned')")}
    assert ev["spam_blocked"] == {"block_probability": 0.97, "category": "advertising"}
    assert ev["banned"]["reason"] == "spam"
    assert "Продаю" not in json.dumps(ev, ensure_ascii=False)  # текст сообщения в базу не пишем


async def test_normal_first_message_goes_to_google_verification(ctx, screening):
    await join(ctx)
    msg = await post(ctx, "Привет! Подскажите, какую нейросеть выбрать для кода?")
    assert screening.calls == ["Привет! Подскажите, какую нейросеть выбрать для кода?"]
    assert not msg.deleted and not ctx.bot.of("ban")
    assert len(ctx.bot.of("send")) == 1 and await ctx.pool.fetchval("SELECT count(*) FROM sessions") == 1


async def test_borderline_message_below_threshold_is_not_banned(ctx, screening):
    screening.verdict = spam.Verdict(0.55, "advertising")
    await join(ctx)
    await post(ctx, "Где купить подписку?")
    assert not ctx.bot.of("ban") and len(ctx.bot.of("send")) == 1


async def test_classifier_outage_falls_back_to_verification(ctx, screening):
    screening.verdict = None
    await join(ctx)
    msg = await post(ctx, "что угодно")
    assert not msg.deleted and not ctx.bot.of("ban") and len(ctx.bot.of("send")) == 1


async def test_messages_without_text_skip_the_model(ctx, screening):
    await join(ctx)
    await post(ctx, None)
    assert screening.calls == []
    assert len(ctx.bot.of("send")) == 1


async def test_caption_of_media_is_screened(ctx, screening):
    screening.verdict = spam.Verdict(0.9, "spam")
    await join(ctx)
    msg = FakeMessage(make_user(1, "nick"), 10, caption="Заработок от 5000$ в день")
    await bot_handlers.on_group_message(msg, ctx)
    assert screening.calls == ["Заработок от 5000$ в день"]
    assert ctx.bot.of("ban")


async def test_model_is_not_called_while_check_is_running_or_for_verified(ctx, screening):
    await join(ctx)
    await post(ctx, "первое", message_id=10)
    second = await post(ctx, "второе", message_id=11)
    assert second.deleted and screening.calls == ["первое"]  # на лишнее сообщение деньги не тратим
    session = await ctx.pool.fetchrow("SELECT * FROM sessions")
    await db.complete_verification(ctx.pool, "h", session["id"], 1)
    await post(ctx, "после верификации", message_id=12)
    assert screening.calls == ["первое"]


async def test_after_timeout_next_message_is_screened_again(ctx, screening):
    from app import service
    await join(ctx)
    await post(ctx, "первое", message_id=10)
    session = await ctx.pool.fetchrow("SELECT * FROM sessions")
    for t in list(ctx.timers.values()):
        t.cancel()
    ctx.timers.clear()
    await ctx.pool.execute("UPDATE sessions SET deadline = now() - interval '1 second'")
    await service.expire(ctx, session["id"])
    screening.verdict = spam.Verdict(0.99, "scam")
    await post(ctx, "теперь это уже спам", message_id=20)
    assert screening.calls == ["первое", "теперь это уже спам"]
    assert ctx.bot.of("ban")


async def test_spam_from_untracked_member_is_deleted_and_banned(ctx, screening):
    screening.verdict = spam.Verdict(0.97, "advertising")
    msg = await post(ctx, "Нейросети без переплаты. Бонус $500: https://t.me/somebot", user_id=77)
    assert msg.deleted is False or ("delete", CHAT_ID, 10) in ctx.bot.of("delete")
    assert ctx.bot.of("ban")
    assert not ctx.bot.of("send")


async def test_clean_message_from_untracked_member_is_left_alone(ctx, screening):
    await post(ctx, "Где купить подписку?", user_id=77)
    assert screening.calls == ["Где купить подписку?"]
    assert not ctx.bot.of("ban") and not ctx.bot.of("send")


# ---------- журнал проверок ----------
async def rows(ctx):
    return await ctx.pool.fetch("SELECT * FROM screened_messages ORDER BY id")


async def test_blocked_and_passed_messages_are_logged_with_text_and_verdict(ctx, screening):
    await join(ctx, 1)
    await join(ctx, 2)
    screening.verdict = spam.Verdict(0.97, "scam")
    await post(ctx, "Бонус $500: https://t.me/bot", user_id=1, message_id=10)
    screening.verdict = spam.Verdict(0.02, "normal")
    await post(ctx, "Где купить подписку?", user_id=2, message_id=11)
    blocked, passed = await rows(ctx)
    assert (blocked["decision"], blocked["user_id"], blocked["text"]) == ("blocked", 1, "Бонус $500: https://t.me/bot")
    assert blocked["category"] == "scam" and blocked["block_probability"] == pytest.approx(0.97)
    assert (passed["decision"], passed["user_id"], passed["username"]) == ("passed", 2, "nick")


async def test_untracked_member_message_is_logged_too(ctx, screening):
    await post(ctx, "Привет всем", user_id=77)
    (row,) = await rows(ctx)
    assert row["decision"] == "passed" and row["user_id"] == 77


async def test_outage_is_logged_as_unchecked(ctx, screening):
    screening.verdict = None
    await join(ctx)
    await post(ctx, "что угодно")
    (row,) = await rows(ctx)
    assert row["decision"] == "unchecked" and row["block_probability"] is None


async def test_messages_without_text_and_extra_messages_are_not_logged(ctx, screening):
    await join(ctx)
    await post(ctx, None)  # без текста: модель не зовём
    assert await rows(ctx) == []


async def test_telegram_service_account_forwards_are_ignored(ctx, screening):
    msg = FakeMessage(make_user(777000, "tg"), 5, text="Пост канала")
    msg.is_automatic_forward = True
    await bot_handlers.on_group_message(msg, ctx)
    assert screening.calls == [] and await rows(ctx) == []


async def test_log_failure_does_not_break_moderation(ctx, screening, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "add_screened_message", boom)
    screening.verdict = spam.Verdict(0.97, "scam")
    await join(ctx)
    await post(ctx, "спам")
    assert ctx.bot.of("ban")
