-- Схема для анти-спам бота (Telegram + Google-верификация через n8n)
-- Выполнять в базе Railway Postgres. Скрипт идемпотентный: повторный запуск безопасен.

CREATE TABLE IF NOT EXISTS members (
    user_id      BIGINT PRIMARY KEY,
    chat_id      BIGINT NOT NULL,
    username     TEXT,
    first_name   TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'verified', 'failed', 'left')),
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at  TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id             BIGINT NOT NULL,
    user_id             BIGINT NOT NULL REFERENCES members(user_id),
    status              TEXT NOT NULL DEFAULT 'waiting'
                        CHECK (status IN ('waiting', 'verified', 'expired')),
    trigger_message_id  BIGINT,
    prompt_message_id   BIGINT,
    deadline            TIMESTAMPTZ NOT NULL,
    last_rendered_sec   INT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sessions_waiting_deadline_idx
    ON sessions (deadline) WHERE status = 'waiting';
-- Не больше одной активной сессии на пользователя (защита от гонки двух сообщений)
CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_waiting_per_user_idx
    ON sessions (user_id) WHERE status = 'waiting';

CREATE TABLE IF NOT EXISTS oauth_states (
    state       TEXT PRIMARY KEY,
    session_id  UUID NOT NULL REFERENCES sessions(id),
    user_id     BIGINT NOT NULL REFERENCES members(user_id),
    status      TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'used')),
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS google_identities (
    google_sub_hash  TEXT PRIMARY KEY,
    user_id          BIGINT NOT NULL REFERENCES members(user_id),
    verified_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    type        TEXT NOT NULL,
    chat_id     BIGINT,
    user_id     BIGINT,
    session_id  UUID,
    payload     JSONB
);
CREATE INDEX IF NOT EXISTS events_user_idx ON events (user_id, created_at);
