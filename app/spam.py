"""Антиспам-классификатор первого сообщения новичка: модель Jev (TypeSafe) через OpenRouter.

Один быстрый запрос на сообщение. Если сервис недоступен или ключ не задан, возвращаем None:
сообщение просто идёт дальше обычной проверкой через Google (мы не блокируем «на всякий случай»).
"""
import logging
from dataclasses import dataclass

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

URL = "https://openrouter.ai/api/alpha/decisions"
TIMEOUT_SEC = 8.0
MAX_TEXT_CHARS = 3000

CONTEXT = (
    "This is the first message that a newly joined member posted in a public Russian-language "
    "Telegram community chat about neural networks and AI."
)

QUESTIONS = {
    # Главный вопрос: по нему принимается решение о блокировке
    "should_block": {
        "type": "noul",
        "instructions": CONTEXT + " Should this message be blocked as spam, advertising or a scam?",
        "criteria": {
            "true": (
                "Spam, advertising or fraud: promotion of products, services, courses, channels, chats, "
                "bots, jobs or earning schemes (even if polite or with a discount); crypto, investment "
                "or easy-money offers; phishing or suspicious links; mass-style or meaningless text; "
                "lures like 'write me in private messages'; adult content promotion."
            ),
            "false": (
                "A normal message from a real person: greeting, introduction, question, answer, "
                "discussion, opinion or request for help, including on-topic questions about AI tools, "
                "models and services."
            ),
        },
    },
    # Только для журнала: помогает разбирать спорные случаи
    "category": {
        "type": "choice",
        "instructions": CONTEXT + " What kind of message is it?",
        "criteria": {
            "normal": "An ordinary message from a real member of the community.",
            "advertising": "Promotion of a product, service, course, channel, chat, bot or job.",
            "spam": "Mass-style or meaningless text, link dumps, lures to private messages, adult content.",
            "scam": "Fraud: fake giveaways, crypto or investment schemes, phishing, impersonation.",
        },
    },
}


@dataclass(frozen=True)
class Verdict:
    block_probability: float  # вероятность, что сообщение надо блокировать (0..1)
    category: str | None  # normal / advertising / spam / scam, только для журнала

    def should_block(self, threshold: float) -> bool:
        return self.block_probability >= threshold


def has_links(message) -> bool:
    entities = list(message.entities or []) + list(message.caption_entities or [])
    return any(getattr(e.type, "value", e.type) in ("url", "text_link") for e in entities)


async def classify(settings: Settings, text: str, *, author: str, contains_link: bool,
                   client: httpx.AsyncClient | None = None) -> Verdict | None:
    """None, если проверка отключена или недоступна (тогда сообщение идёт обычным путём)."""
    if not settings.openrouter_api_key:
        return None
    payload = {
        "model": settings.spam_model,
        "state": {
            "message": text[:MAX_TEXT_CHARS],
            "author_name": author,
            "contains_link": contains_link,
        },
        "questions": QUESTIONS,
    }
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}", "Content-Type": "application/json"}
    http = client or httpx.AsyncClient(timeout=TIMEOUT_SEC)
    try:
        response = await http.post(URL, json=payload, headers=headers)
        if response.status_code != 200:
            log.warning("Антиспам: OpenRouter ответил %s: %s", response.status_code, response.text[:200])
            return None
        answers = response.json()["answers"]
        category = (answers.get("category") or {}).get("choice")
        return Verdict(float(answers["should_block"]["noul"]), category)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        log.warning("Антиспам-проверка недоступна: %r", e)
        return None
    finally:
        if client is None:
            await http.aclose()
