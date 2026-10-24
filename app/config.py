"""Настройки из переменных окружения."""
import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    chat_id: int
    database_url: str
    google_client_id: str
    google_client_secret: str
    public_base_url: str
    secret_key: str
    port: int
    verify_timeout_sec: int
    max_failed_attempts: int
    run_migrations: bool
    # антиспам-классификатор (необязателен: без ключа проверка отключена)
    openrouter_api_key: str | None = None
    spam_model: str = "typesafe/jev-1.13"
    spam_threshold: float = 0.8

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url}/auth/google/callback"


def load_settings() -> Settings:
    return Settings(
        bot_token=_required("BOT_TOKEN"),
        chat_id=int(_required("CHAT_ID")),
        database_url=_required("DATABASE_URL"),
        google_client_id=_required("GOOGLE_CLIENT_ID"),
        google_client_secret=_required("GOOGLE_CLIENT_SECRET"),
        public_base_url=_required("PUBLIC_BASE_URL").rstrip("/"),
        secret_key=_required("SECRET_KEY"),
        port=int(os.environ.get("PORT", "8000")),
        verify_timeout_sec=int(os.environ.get("VERIFY_TIMEOUT_SEC", "60")),
        max_failed_attempts=int(os.environ.get("MAX_FAILED_ATTEMPTS", "3")),
        run_migrations=os.environ.get("RUN_MIGRATIONS", "") == "1",
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", "").strip() or None,
        spam_model=os.environ.get("SPAM_MODEL", "typesafe/jev-1.13"),
        spam_threshold=float(os.environ.get("SPAM_THRESHOLD", "0.8")),
    )
