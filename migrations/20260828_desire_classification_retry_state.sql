ALTER TABLE desire_pending_classifications
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_desire_pending_due
    ON desire_pending_classifications(status,next_retry_at,created_at);

CREATE TABLE IF NOT EXISTS desire_classification_dead_letters (
    id                       TEXT PRIMARY KEY,
    text                     TEXT NOT NULL,
    context                  TEXT NOT NULL DEFAULT '',
    intimate_scene_open      BOOLEAN NOT NULL DEFAULT FALSE,
    intimate_scene_id        TEXT NOT NULL DEFAULT '',
    intimate_window_minutes  INTEGER NOT NULL DEFAULT 0,
    current_implicit         BOOLEAN NOT NULL DEFAULT FALSE,
    attempt_count            INTEGER NOT NULL,
    last_error               TEXT NOT NULL DEFAULT '',
    created_at               TIMESTAMPTZ NOT NULL,
    dead_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
