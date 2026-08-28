BEGIN;
CREATE TABLE IF NOT EXISTS desire_state (
  id INTEGER PRIMARY KEY DEFAULT 1, drives JSONB NOT NULL,
  last_tick_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), CHECK (id=1)
);
CREATE TABLE IF NOT EXISTS desire_thoughts (
  id BIGSERIAL PRIMARY KEY, text TEXT NOT NULL, drive_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('flit','fixation')), strength REAL NOT NULL,
  fed_count INTEGER NOT NULL DEFAULT 0, born_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_desire_thoughts_kind_strength ON desire_thoughts(kind,strength DESC);
CREATE TABLE IF NOT EXISTS desire_pulses (
  id BIGSERIAL PRIMARY KEY, event_type TEXT NOT NULL, drive_key TEXT, delta REAL,
  source_ref TEXT, meta JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_desire_pulses_created ON desire_pulses(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_desire_pulses_idempotency ON desire_pulses(event_type,source_ref,created_at DESC);
ALTER TABLE proactive_push_outbox ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'haiku_proactive';
ALTER TABLE proactive_push_outbox ADD COLUMN IF NOT EXISTS intent JSONB;
COMMIT;
