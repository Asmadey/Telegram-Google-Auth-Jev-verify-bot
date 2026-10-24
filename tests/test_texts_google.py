import uuid
from urllib.parse import parse_qs, urlparse

from app import google, texts
from app.config import Settings


def make_settings():
    return Settings("t", -1, "db", "cid", "sec", "https://app.test", "k" * 32, 8000, 60, 3, False)


def test_mention_prefers_username_and_escapes():
    assert texts.mention(5, "nick_1", "Nick") == "@nick_1"
    link = texts.mention(5, None, "<b>Ник</b>")
    assert link == '<a href="tg://user?id=5">&lt;b&gt;Ник&lt;/b&gt;</a>'


def test_prompt_text_has_countdown():
    text = texts.prompt_text("@nick", 59)
    assert "@nick" in text and "59 сек." in text


def test_deep_link_and_keyboard():
    sid = uuid.uuid4()
    kb = texts.prompt_keyboard("mybot", sid)
    button = kb.inline_keyboard[0][0]
    assert button.text == "Авторизоваться"
    assert button.url == f"https://t.me/mybot?start={sid}"
    assert len(str(sid)) <= 64  # лимит Telegram на payload /start


def test_google_auth_url_params():
    s = make_settings()
    q = parse_qs(urlparse(google.build_auth_url(s, "STATE")).query)
    assert q["client_id"] == ["cid"]
    assert q["redirect_uri"] == ["https://app.test/auth/google/callback"]
    assert q["state"] == ["STATE"] and q["scope"] == ["openid email"]


def test_sub_hash_is_stable_and_hides_sub():
    s = make_settings()
    h = google.sub_hash(s, "1234567890")
    assert h == google.sub_hash(s, "1234567890") and "1234567890" not in h and len(h) == 64
