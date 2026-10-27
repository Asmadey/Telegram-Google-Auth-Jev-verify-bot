-- Журнал сообщений, которые проверяла модель: что заблокировано, а что прошло.
CREATE TABLE IF NOT EXISTS screened_messages (
    id                 BIGSERIAL PRIMARY KEY,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    chat_id            BIGINT NOT NULL,
    user_id            BIGINT NOT NULL,
    username           TEXT,
    first_name         TEXT,
    message_id         BIGINT,
    content_type       TEXT,
    text               TEXT NOT NULL,
    has_link           BOOLEAN NOT NULL DEFAULT false,
    block_probability  DOUBLE PRECISION,
    category           TEXT,
    decision           TEXT NOT NULL CHECK (decision IN ('blocked', 'passed', 'unchecked'))
);
CREATE INDEX IF NOT EXISTS screened_messages_created_idx ON screened_messages (created_at DESC);
CREATE INDEX IF NOT EXISTS screened_messages_user_idx ON screened_messages (user_id, created_at);
