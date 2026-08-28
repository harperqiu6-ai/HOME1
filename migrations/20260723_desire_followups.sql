CREATE TABLE IF NOT EXISTS desire_followups (
    id              BIGSERIAL PRIMARY KEY,
    event_key       TEXT NOT NULL UNIQUE,
    question_text   TEXT NOT NULL,
    kind            TEXT NOT NULL,
    drive_key       TEXT NOT NULL,
    thought_text    TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    confidence      REAL NOT NULL DEFAULT 0,
    intensity       REAL NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 1,
    next_due_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    queued_at       TIMESTAMPTZ,
    last_asked_at   TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT desire_followups_kind_check
        CHECK (kind IN ('question', 'reminder', 'request')),
    CONSTRAINT desire_followups_drive_check
        CHECK (drive_key IN ('attachment', 'reflection', 'duty')),
    CONSTRAINT desire_followups_status_check
        CHECK (status IN ('pending', 'awaiting_answer', 'deferred', 'resolved', 'cancelled', 'exhausted'))
);

CREATE INDEX IF NOT EXISTS idx_desire_followups_due
ON desire_followups (drive_key, next_due_at, created_at)
WHERE status IN ('pending', 'deferred', 'awaiting_answer');
