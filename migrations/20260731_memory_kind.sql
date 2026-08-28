BEGIN;

ALTER TABLE memories
ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'fact';

UPDATE memories
SET kind = 'musing',
    is_active = TRUE,
    decayed_at = NULL
WHERE content LIKE '【V的随想】%'
  AND kind <> 'musing';

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'memories_kind_check'
    ) THEN
        ALTER TABLE memories
        ADD CONSTRAINT memories_kind_check
        CHECK (kind IN ('fact', 'musing'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories (kind);

COMMIT;
