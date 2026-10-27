"""Жизненный цикл сессии верификации: запрос, обратный отсчёт, успех, истечение, блокировка."""
import asyncio
import logging
import math
import uuid

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import ReplyParameters

from app import db, texts
from app.context import Ctx

log = logging.getLogger(__name__)

TICK_SEC = 1.0
SUCCESS_DELETE_DELAY_SEC = 5
MAX_RETRY_AFTER_SEC = 5  # если Telegram просит ждать дольше, правку пропускаем


async def _safe_delete(ctx: Ctx, chat_id: int, message_id: int) -> None:
    try:
        await ctx.bot.delete_message(chat_id, message_id)
    except TelegramAPIError as e:
        log.warning("Не удалось удалить сообщение %s: %s", message_id, e)


async def _edit(ctx: Ctx, session, text: str, markup) -> bool:
    """Правит сообщение бота. False, если сообщения больше нет или правка невозможна."""
    try:
        await ctx.bot.edit_message_text(
            text=text,
            chat_id=session["chat_id"],
            message_id=session["prompt_message_id"],
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )
        return True
    except TelegramRetryAfter as e:
        # Лимит Telegram: ждём, если недолго, иначе пропускаем эту секунду
        if e.retry_after <= MAX_RETRY_AFTER_SEC:
            await asyncio.sleep(e.retry_after)
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return True
        log.warning("Правка сообщения %s отклонена: %s", session["prompt_message_id"], e)
        return False
    except TelegramAPIError as e:
        log.warning("Ошибка правки сообщения %s: %s", session["prompt_message_id"], e)
        return False


def _spawn(ctx: Ctx, coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    ctx.background.add(task)
    task.add_done_callback(ctx.background.discard)
    return task


async def start_session(ctx: Ctx, member, trigger_message_id: int):
    """Создаёт сессию, отвечает реплаем на сообщение новичка, запускает отсчёт.

    None, если у пользователя уже есть активная сессия.
    """
    s = ctx.settings
    session = await db.create_session(
        ctx.pool, s.chat_id, member["user_id"], trigger_message_id, s.verify_timeout_sec
    )
    if session is None:
        return None
    who = texts.mention(member["user_id"], member["username"], member["first_name"])
    try:
        sent = await ctx.bot.send_message(
            s.chat_id,
            texts.prompt_text(who, s.verify_timeout_sec),
            reply_parameters=ReplyParameters(
                message_id=trigger_message_id, allow_sending_without_reply=True
            ),
            reply_markup=texts.prompt_keyboard(ctx.bot_username, session["id"]),
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        await db.expire_session(ctx.pool, session["id"])
        raise
    await db.set_prompt_message(ctx.pool, session["id"], sent.message_id)
    await db.add_event(ctx.pool, "prompt_sent", chat_id=s.chat_id,
                       user_id=member["user_id"], session_id=session["id"])
    start_timer(ctx, session["id"], s.verify_timeout_sec)
    return session


def start_timer(ctx: Ctx, session_id: uuid.UUID, initial_rendered: int | None) -> None:
    key = str(session_id)
    if key in ctx.timers:
        return
    ctx.timers[key] = asyncio.create_task(_run_timer(ctx, session_id, initial_rendered))


async def _run_timer(ctx: Ctx, session_id: uuid.UUID, last_rendered: int | None) -> None:
    key = str(session_id)
    try:
        while True:
            session = await db.get_session(ctx.pool, session_id)
            if session is None or session["status"] != "waiting":
                return
            remaining = math.ceil(session["seconds_left"])
            if remaining <= 0:
                await expire(ctx, session_id)
                return
            if remaining != last_rendered and session["prompt_message_id"] is not None:
                who = texts.mention(session["user_id"], session["username"], session["first_name"])
                await _edit(
                    ctx, session, texts.prompt_text(who, remaining),
                    texts.prompt_keyboard(ctx.bot_username, session_id),
                )
                last_rendered = remaining
            await asyncio.sleep(TICK_SEC)
    except asyncio.CancelledError:  # pylint: disable=try-except-raise  # отмену не логируем как сбой
        raise
    except Exception:  # pylint: disable=broad-exception-caught  # сбой таймера не должен ронять бота
        log.exception("Сбой таймера сессии %s", session_id)
    finally:
        if ctx.timers.get(key) is asyncio.current_task():
            ctx.timers.pop(key, None)


async def expire(ctx: Ctx, session_id: uuid.UUID) -> None:
    """Время вышло: удаляем сообщение бота и сообщение новичка; после N неудач блокируем."""
    row = await db.expire_session(ctx.pool, session_id)
    if row is None:
        return  # уже завершена (например, верификация успела раньше)
    if row["prompt_message_id"]:
        await _safe_delete(ctx, row["chat_id"], row["prompt_message_id"])
    if row["trigger_message_id"]:
        await _safe_delete(ctx, row["chat_id"], row["trigger_message_id"])
    await db.add_event(ctx.pool, "expired", chat_id=row["chat_id"],
                       user_id=row["user_id"], session_id=session_id)

    failed = await db.count_failed_attempts(ctx.pool, row["user_id"])
    if failed >= ctx.settings.max_failed_attempts:
        await _ban(ctx, row["chat_id"], row["user_id"], {"failed_attempts": failed})


async def _ban(ctx: Ctx, chat_id: int, user_id: int, payload: dict, revoke_messages: bool = False) -> None:
    """Блокировка в группе. Разбанить можно вручную в Telegram."""
    try:
        await ctx.bot.ban_chat_member(chat_id, user_id, revoke_messages=revoke_messages)
    except TelegramAPIError as e:
        log.error("Не удалось заблокировать %s: %s", user_id, e)
        return
    await db.mark_member_failed(ctx.pool, user_id)
    await db.add_event(ctx.pool, "banned", chat_id=chat_id, user_id=user_id, payload=payload)


async def block_spammer(ctx: Ctx, message, verdict) -> None:
    """Первое сообщение признано спамом или рекламой: удаляем его и блокируем автора сразу."""
    user_id, chat_id = message.from_user.id, ctx.settings.chat_id
    await _safe_delete(ctx, chat_id, message.message_id)
    details = {"block_probability": round(verdict.block_probability, 3), "category": verdict.category}
    await db.add_event(ctx.pool, "spam_blocked", chat_id=chat_id, user_id=user_id, payload=details)
    # revoke_messages: заодно убираем всё, что этот пользователь успел написать
    await _ban(ctx, chat_id, user_id, {"reason": "spam", **details}, revoke_messages=True)


async def finish_verified(ctx: Ctx, session) -> None:
    """Успех: останавливаем таймер, меняем сообщение бота и удаляем его через 5 секунд."""
    task = ctx.timers.pop(str(session["id"]), None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    message_id = session["prompt_message_id"]
    if not message_id:
        return
    await _edit(ctx, session, texts.SUCCESS_TEXT, None)

    async def _delete_later():
        await asyncio.sleep(SUCCESS_DELETE_DELAY_SEC)
        await _safe_delete(ctx, session["chat_id"], message_id)

    _spawn(ctx, _delete_later())


async def recover(ctx: Ctx) -> None:
    """После перезапуска продолжаем таймеры активных сессий (просроченные будут закрыты сразу)."""
    for row in await db.list_waiting_sessions(ctx.pool):
        start_timer(ctx, row["id"], None)
