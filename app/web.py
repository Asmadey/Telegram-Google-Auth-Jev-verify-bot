"""Веб-часть: возврат из Google (callback) и проверка живости сервиса.

Итог входа бот присылает в личку, а браузер сразу перенаправляется в бота (отдельной страницы нет).
Страница показывается только тогда, когда пользователя нельзя определить (нет или недействителен state).
"""
import html
import logging

import httpx
from aiogram.exceptions import TelegramAPIError
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db, google, service

log = logging.getLogger(__name__)

MSG_OK = "✅ Вы верифицированы! Теперь можно писать в группе."
MSG_EXPIRED = "⌛ Время вышло. Напишите в группе ещё раз, и бот пришлёт новую ссылку."
MSG_TAKEN = "⚠️ Этот Google-аккаунт уже привязан к другому участнику. Войдите другим аккаунтом."
MSG_NO_EMAIL = "⚠️ Email этого Google-аккаунта не подтверждён. Войдите другим аккаунтом."
MSG_FAILED = ("⚠️ Google не подтвердил вход. Нажмите «Авторизоваться» в группе ещё раз, "
              "пока не вышло время.")


def _page(title: str, text: str, status: int = 200, bot_username: str | None = None) -> HTMLResponse:
    link = (f"<p><a href='https://t.me/{html.escape(bot_username)}'>Вернуться в бота</a></p>"
            if bot_username else "")
    body = (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:-apple-system,system-ui,sans-serif;max-width:28rem;"
        "margin:20vh auto;padding:0 1rem;text-align:center;color:#222}</style></head>"
        f"<body><h2>{html.escape(title)}</h2><p>{html.escape(text)}</p>{link}</body></html>"
    )
    return HTMLResponse(body, status_code=status)


async def _notify_and_return(ctx, user_id: int, text: str) -> RedirectResponse:
    """Статус в личку бота и сразу назад в бота."""
    try:
        await ctx.bot.send_message(user_id, text)
    except TelegramAPIError as e:
        log.warning("Не удалось отправить статус пользователю %s: %s", user_id, e)
    return RedirectResponse(f"https://t.me/{ctx.bot_username}", status_code=302)


def create_app(lifespan=None) -> FastAPI:
    """Контекст (база, бот, настройки) кладётся в app.state.ctx при старте приложения."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/auth/google/callback")
    async def google_callback(request: Request, code: str | None = None, state: str | None = None,
                              error: str | None = None):
        ctx = request.app.state.ctx
        if error or not code or not state:
            return _page("Вход отменён", "Вернитесь в Telegram и нажмите «Авторизоваться» ещё раз.",
                         400, ctx.bot_username)

        st = await db.consume_oauth_state(ctx.pool, state)
        if st is None:
            return _page("Ссылка недействительна",
                         "Ссылка устарела или уже использована. Запросите новую у бота.",
                         400, ctx.bot_username)
        user_id = st["user_id"]

        try:
            identity = await google.fetch_identity(ctx.settings, code)
        except httpx.HTTPError:
            log.exception("Сбой при обращении к Google")
            identity = None
        if identity is None:
            return await _notify_and_return(ctx, user_id, MSG_FAILED)
        if identity.get("email_verified") not in (True, "true") or not identity.get("sub"):
            return await _notify_and_return(ctx, user_id, MSG_NO_EMAIL)

        result, session = await db.complete_verification(
            ctx.pool, google.sub_hash(ctx.settings, str(identity["sub"])),
            st["session_id"], user_id,
        )
        if result == "taken":
            return await _notify_and_return(ctx, user_id, MSG_TAKEN)
        if result != "ok":
            return await _notify_and_return(ctx, user_id, MSG_EXPIRED)

        await service.finish_verified(ctx, session)
        return await _notify_and_return(ctx, user_id, MSG_OK)

    return app
