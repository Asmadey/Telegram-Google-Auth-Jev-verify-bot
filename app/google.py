"""Google OAuth: ссылка авторизации, обмен кода, хэш идентификатора."""
import hashlib
import hmac
from urllib.parse import urlencode

import httpx

from app.config import Settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def build_auth_url(settings: Settings, state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.redirect_uri,
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def sub_hash(settings: Settings, sub: str) -> str:
    """Сырой Google ID в базе не храним: только HMAC с секретом приложения."""
    return hmac.new(settings.secret_key.encode(), sub.encode(), hashlib.sha256).hexdigest()


async def fetch_identity(settings: Settings, code: str) -> dict | None:
    """Меняет код на токен и возвращает userinfo. None, если Google отказал."""
    async with httpx.AsyncClient(timeout=10) as http:
        token = await http.post(TOKEN_URL, data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.redirect_uri,
            "grant_type": "authorization_code",
        })
        if token.status_code != 200:
            return None
        access_token = token.json().get("access_token")
        if not access_token:
            return None
        info = await http.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        if info.status_code != 200:
            return None
        return info.json()
