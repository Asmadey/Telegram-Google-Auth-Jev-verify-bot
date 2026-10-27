"""Точка входа: ASGI-приложение, в lifespan которого запускается бот (polling).

Работают оба способа старта:
    python main.py
    uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from aiogram import Bot, Dispatcher

from app import db, service
from app.bot import router
from app.config import load_settings
from app.context import Ctx
from app.web import create_app

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
log = logging.getLogger("main")


def _exit_if_polling_failed(task: asyncio.Task) -> None:
    """Без polling бот бесполезен: падаем, чтобы Railway перезапустил сервис."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error("Polling Telegram остановился: %r", error)
        os._exit(1)


@asynccontextmanager
async def lifespan(app):  # pylint: disable=redefined-outer-name
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    pool = await db.create_pool(settings.database_url)
    try:
        if settings.run_migrations:
            await db.migrate(pool, MIGRATIONS_DIR)
        elif not await db.schema_ready(pool):
            raise RuntimeError(
                "Схема базы не применена, а автоматические миграции отключены (RUN_MIGRATIONS=0). "
                "Уберите переменную или примените migrations/*.sql вручную."
            )

        bot = Bot(settings.bot_token)
        try:
            me = await bot.get_me()
            ctx = Ctx(settings=settings, pool=pool, bot=bot, bot_username=me.username)
            app.state.ctx = ctx

            dp = Dispatcher(ctx=ctx)
            dp.include_router(router)
            await service.recover(ctx)
            poll = asyncio.create_task(
                dp.start_polling(bot, allowed_updates=["message", "chat_member"], handle_signals=False)
            )
            poll.add_done_callback(_exit_if_polling_failed)
            log.info("Бот @%s запущен, чат %s", me.username, settings.chat_id)
            try:
                yield
            finally:
                poll.cancel()
                await asyncio.gather(poll, return_exceptions=True)
                for task in list(ctx.timers.values()) + list(ctx.background):
                    task.cancel()
        finally:
            await bot.session.close()
    finally:
        await pool.close()


app = create_app(lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
