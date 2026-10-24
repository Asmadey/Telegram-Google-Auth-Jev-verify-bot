"""Общий контекст приложения: передаётся в обработчики бота и веб-сервера."""
import asyncio
from dataclasses import dataclass, field

import asyncpg
from aiogram import Bot

from app.config import Settings


@dataclass
class Ctx:
    settings: Settings
    pool: asyncpg.Pool
    bot: Bot
    bot_username: str
    # session_id (str) -> задача таймера; нужна, чтобы остановить отсчёт при успешной верификации
    timers: dict[str, asyncio.Task] = field(default_factory=dict)
    # ссылки на фоновые задачи, чтобы их не собрал сборщик мусора
    background: set[asyncio.Task] = field(default_factory=set)
