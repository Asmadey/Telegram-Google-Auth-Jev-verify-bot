"""Обработчики Telegram: вступление, первое сообщение новичка, выдача ссылки Google в личке."""
import logging
import secrets
import uuid

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import ChatMemberUpdated, Message

from app import db, google, service, spam, texts
from app.context import Ctx

log = logging.getLogger(__name__)
router = Router()

GONE = {"left", "kicked"}
TELEGRAM_SERVICE_ID = 777000  # служебный аккаунт Telegram: от его имени приходят автопересылки из канала

# Служебные сообщения Telegram («X вступил», «закреплено» и т. п.): они не считаются первым сообщением.
# Всё остальное, включая неизвестные и новые типы (story, платное медиа, кубик, игра, розыгрыш...),
# считается сообщением пользователя: для антиспама безопаснее проверять всё, чем пропускать неизвестное.
SERVICE_CONTENT_NAMES = (
    "NEW_CHAT_MEMBERS", "LEFT_CHAT_MEMBER", "NEW_CHAT_TITLE", "NEW_CHAT_PHOTO", "DELETE_CHAT_PHOTO",
    "GROUP_CHAT_CREATED", "SUPERGROUP_CHAT_CREATED", "CHANNEL_CHAT_CREATED",
    "MESSAGE_AUTO_DELETE_TIMER_CHANGED", "MIGRATE_TO_CHAT_ID", "MIGRATE_FROM_CHAT_ID", "PINNED_MESSAGE",
    "FORUM_TOPIC_CREATED", "FORUM_TOPIC_EDITED", "FORUM_TOPIC_CLOSED", "FORUM_TOPIC_REOPENED",
    "GENERAL_FORUM_TOPIC_HIDDEN", "GENERAL_FORUM_TOPIC_UNHIDDEN",
    "VIDEO_CHAT_SCHEDULED", "VIDEO_CHAT_STARTED", "VIDEO_CHAT_ENDED", "VIDEO_CHAT_PARTICIPANTS_INVITED",
    "BOOST_ADDED", "CHAT_BACKGROUND_SET", "WRITE_ACCESS_ALLOWED", "PROXIMITY_ALERT_TRIGGERED",
    "USER_SHARED", "USERS_SHARED", "CHAT_SHARED", "SUCCESSFUL_PAYMENT", "REFUNDED_PAYMENT",
    "GIVEAWAY_CREATED", "GIVEAWAY_COMPLETED", "GIVEAWAY_WINNERS", "GIFT_UPGRADE_SENT",
    "CHECKLIST_TASKS_DONE", "CHECKLIST_TASKS_ADDED", "POLL_OPTION_ADDED", "POLL_OPTION_DELETED",
    "CHAT_OWNER_CHANGED", "CHAT_OWNER_LEFT", "COMMUNITY_CHAT_ADDED", "COMMUNITY_CHAT_JOINED",
    "COMMUNITY_CHAT_REMOVED", "DIRECT_MESSAGE_PRICE_CHANGED", "PAID_MESSAGE_PRICE_CHANGED",
    "SUGGESTED_POST_APPROVED", "SUGGESTED_POST_APPROVAL_FAILED", "SUGGESTED_POST_DECLINED",
    "SUGGESTED_POST_PAID", "SUGGESTED_POST_REFUNDED", "MANAGED_BOT_CREATED", "PASSPORT_DATA",
    "CONNECTED_WEBSITE",
)
# getattr: в разных версиях aiogram набор типов разный, отсутствующие имена пропускаем
SERVICE_CONTENT = {getattr(ContentType, n) for n in SERVICE_CONTENT_NAMES if hasattr(ContentType, n)}
USER_MESSAGE = ~F.content_type.in_(SERVICE_CONTENT)


class MessageTrace(BaseMiddleware):
    """Строка в лог на каждое сообщение группы (без текста): видно, что получил бот и от кого."""

    async def __call__(self, handler, event, data):
        if event.chat.type in ("group", "supergroup"):
            user = event.from_user
            log.info(
                "сообщение: чат=%s тип=%s from=%s bot=%s sender_chat=%s auto_fwd=%s",
                event.chat.id, getattr(event.content_type, "value", event.content_type),
                user.id if user else None, user.is_bot if user else None,
                event.sender_chat.id if event.sender_chat else None, bool(event.is_automatic_forward),
            )
        return await handler(event, data)


router.message.outer_middleware(MessageTrace())


def status_of(member) -> str:
    """Строковый статус участника (в aiogram это enum, у которого хэш не совпадает с хэшем строки)."""
    status = member.status
    return getattr(status, "value", status)


@router.chat_member()
async def on_chat_member(event: ChatMemberUpdated, ctx: Ctx) -> None:
    if event.chat.id != ctx.settings.chat_id:
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return
    old, new = status_of(event.old_chat_member), status_of(event.new_chat_member)
    if old in GONE and new not in GONE:
        await db.upsert_member_joined(ctx.pool, user.id, event.chat.id, user.username, user.first_name)
        await db.add_event(ctx.pool, "joined", chat_id=event.chat.id, user_id=user.id)
    elif new in GONE and old not in GONE:
        await db.mark_member_left(ctx.pool, user.id)
        await db.add_event(ctx.pool, "left", chat_id=event.chat.id, user_id=user.id)


@router.message(CommandStart(), F.chat.type == "private")
async def on_start(message: Message, command: CommandObject, ctx: Ctx) -> None:
    user = message.from_user
    try:
        session_id = uuid.UUID((command.args or "").strip())
    except ValueError:
        await message.answer(
            "Чтобы пройти проверку, нажмите кнопку «Авторизоваться» в сообщении бота в группе."
        )
        return

    session = await db.get_session(ctx.pool, session_id)
    if (
        session is None
        or session["user_id"] != user.id  # ссылку открыл не тот, для кого она создана
        or session["status"] != "waiting"
        or session["seconds_left"] <= 0
    ):
        await message.answer(
            "Эта ссылка недействительна или время вышло. Напишите в группе, и бот пришлёт новую."
        )
        return

    state = secrets.token_urlsafe(32)
    await db.create_oauth_state(ctx.pool, state, session_id, user.id)
    await message.answer(
        "Нажмите кнопку и войдите через Google.",
        reply_markup=texts.google_button(google.build_auth_url(ctx.settings, state)),
    )
    await db.add_event(ctx.pool, "oauth_started", chat_id=session["chat_id"],
                       user_id=user.id, session_id=session_id)


@router.message(F.chat.type.in_({"group", "supergroup"}), USER_MESSAGE)
async def on_group_message(message: Message, ctx: Ctx) -> None:
    if message.chat.id != ctx.settings.chat_id:
        return
    user = message.from_user
    if user is None or user.is_bot:
        log.info("пропуск: отправитель бот или канал (from=%s sender_chat=%s)",
                 user.id if user else None, message.sender_chat.id if message.sender_chat else None)
        return
    if user.id == TELEGRAM_SERVICE_ID or getattr(message, "is_automatic_forward", False):
        return  # автопересылка поста канала в группу обсуждений: не сообщение участника
    member = await db.get_member(ctx.pool, user.id)
    if member is None:
        # не новичок (был в группе до бота или вступил, пока бот не работал): проверку Google не требуем,
        # но явный спам и рекламу модель всё равно отсекает
        verdict = await _screen_for_spam(ctx, message, user)
        blocked = verdict is not None and verdict.should_block(ctx.settings.spam_threshold)
        await _record_screening(ctx, message, user, verdict, blocked)
        if blocked:
            await service.block_spammer(ctx, message, verdict)
        else:
            log.info("пропуск: пользователь %s не в базе, спама не найдено", user.id)
        return
    if member["status"] != "pending":
        log.info("пропуск: пользователь %s не в очереди проверки (%s)", user.id, member["status"])
        return

    if await db.has_active_session(ctx.pool, user.id):
        await _delete_quietly(message)  # проверка уже идёт: лишнее сообщение убираем, модель не зовём
        return

    verdict = await _screen_for_spam(ctx, message, user)
    blocked = verdict is not None and verdict.should_block(ctx.settings.spam_threshold)
    await _record_screening(ctx, message, user, verdict, blocked)
    if blocked:
        await service.block_spammer(ctx, message, verdict)
        return

    try:
        session = await service.start_session(ctx, member, message.message_id)
    except TelegramAPIError as e:
        log.error("Не удалось отправить запрос верификации: %s", e)
        return
    if session is None:
        # Верификация уже идёт (гонка двух сообщений): лишнее удаляем
        await _delete_quietly(message)


async def _delete_quietly(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError as e:
        log.warning("Не удалось удалить сообщение новичка: %s", e)


async def _screen_for_spam(ctx: Ctx, message: Message, user):
    """Verdict модели или None (нет текста, проверка отключена или недоступна)."""
    text = message.text or message.caption
    if not text or not text.strip():
        return None  # стикер, фото без подписи и т. п.: модель получает только текст
    verdict = await spam.classify(
        ctx.settings, text, author=user.full_name, contains_link=spam.has_links(message)
    )
    if verdict is not None:
        log.info("антиспам: user=%s block_p=%.2f category=%s", user.id, verdict.block_probability,
                 verdict.category)
    return verdict


async def _record_screening(ctx: Ctx, message: Message, user, verdict, blocked: bool) -> None:
    """Пишет в журнал текст и решение модели. Сбой записи не должен мешать модерации."""
    text = message.text or message.caption
    if not text or not text.strip():
        return
    decision = "blocked" if blocked else ("passed" if verdict is not None else "unchecked")
    try:
        await db.add_screened_message(
            ctx.pool, chat_id=message.chat.id, user_id=user.id, username=user.username,
            first_name=user.first_name, message_id=message.message_id,
            content_type=_content_type_name(message),
            text=text, has_link=spam.has_links(message), verdict=verdict, decision=decision,
        )
    except Exception:
        log.exception("Не удалось записать проверку сообщения в журнал")


def _content_type_name(message: Message) -> str | None:
    kind = getattr(message, "content_type", None)
    return str(getattr(kind, "value", kind)) if kind is not None else None
