CREATE TABLE IF NOT EXISTS desire_pending_classifications (
    id                       TEXT PRIMARY KEY,
    text                     TEXT NOT NULL,
    context                  TEXT NOT NULL DEFAULT '',
    intimate_scene_open      BOOLEAN NOT NULL DEFAULT FALSE,
    intimate_scene_id        TEXT NOT NULL DEFAULT '',
    intimate_window_minutes  INTEGER NOT NULL DEFAULT 0,
    current_implicit         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
