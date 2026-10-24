"""Тексты сообщений и разметка. Только чистые функции, без обращений к сети и базе."""
import html
import uuid

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

SUCCESS_TEXT = "🎉 Вы верифицированы!"


def mention(user_id: int, username: str | None, first_name: str | None) -> str:
    """@ник, если он есть, иначе кликабельное имя через tg://user (тоже уведомляет)."""
    if username:
        return f"@{html.escape(username)}"
    name = html.escape(first_name or "участник")
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def prompt_text(mention_html: str, remaining: int) -> str:
    return (
        f"Уважаемый {mention_html}, подтвердите, что вы не бот.\n"
        f"⏳ {remaining} сек. чтобы подтвердить через Google"
    )


def deep_link(bot_username: str, session_id: uuid.UUID) -> str:
    return f"https://t.me/{bot_username}?start={session_id}"


def prompt_keyboard(bot_username: str, session_id: uuid.UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Авторизоваться", url=deep_link(bot_username, session_id))
    ]])


def google_button(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Войти через Google", url=url)
    ]])
