"""PostgreSQL persistence boundary for the desire v1 kernel."""

from __future__ import annotations

import json
from datetime import timedelta

from desire import BASELINES, DesireState, Thought


def _row_with_decoded_meta(row):
    item = dict(row)
    raw_meta = item.get("meta")
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except (TypeError, ValueError):
            raw_meta = None
    item["meta"] = raw_meta if isinstance(raw_meta, dict) else {}
    return item


class DesireStore:
    def __init__(self, pool):
        self.pool = pool

    async def load(self, now) -> tuple[DesireState, object]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT drives, last_tick_at FROM desire_state WHERE id=1")
            thoughts = await conn.fetch("SELECT id,text,drive_key,kind,strength,fed_count,born_at FROM desire_thoughts ORDER BY strength DESC,id")
        if not row:
            return DesireState(), now
        drives = row["drives"] if isinstance(row["drives"], dict) else json.loads(row["drives"])
        return DesireState(dict(drives), [Thought(**dict(item)) for item in thoughts]), row["last_tick_at"]

    async def save(self, state: DesireState, last_tick_at, now):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""INSERT INTO desire_state(id,drives,last_tick_at,updated_at) VALUES(1,$1::jsonb,$2,$3)
                    ON CONFLICT(id) DO UPDATE SET drives=EXCLUDED.drives,last_tick_at=EXCLUDED.last_tick_at,updated_at=EXCLUDED.updated_at""",
                    json.dumps(state.drives), last_tick_at, now)
                await conn.execute("DELETE FROM desire_thoughts")
                if state.thoughts:
                    await conn.executemany("""INSERT INTO desire_thoughts(text,drive_key,kind,strength,fed_count,born_at,updated_at)
                        VALUES($1,$2,$3,$4,$5,$6,$7)""", [(t.text,t.drive_key,t.kind,t.strength,t.fed_count,t.born_at,now) for t in state.thoughts])

    async def save_with_pulses(self, state: DesireState, last_tick_at, pulses, now, delivery_id=""):
        """Commit drive state and its audit rows together; optionally dedupe one delivered event."""
        normalized_delivery_id = str(delivery_id or "").strip()[:240]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if normalized_delivery_id:
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtext($1))", normalized_delivery_id,
                    )
                    duplicate = await conn.fetchval(
                        "SELECT 1 FROM desire_pulses WHERE meta->>'delivery_id'=$1 LIMIT 1",
                        normalized_delivery_id,
                    )
                    if duplicate:
                        return False
                await conn.execute("""INSERT INTO desire_state(id,drives,last_tick_at,updated_at) VALUES(1,$1::jsonb,$2,$3)
                    ON CONFLICT(id) DO UPDATE SET drives=EXCLUDED.drives,last_tick_at=EXCLUDED.last_tick_at,updated_at=EXCLUDED.updated_at""",
                    json.dumps(state.drives), last_tick_at, now)
                await conn.execute("DELETE FROM desire_thoughts")
                if state.thoughts:
                    await conn.executemany("""INSERT INTO desire_thoughts(text,drive_key,kind,strength,fed_count,born_at,updated_at)
                        VALUES($1,$2,$3,$4,$5,$6,$7)""", [(t.text,t.drive_key,t.kind,t.strength,t.fed_count,t.born_at,now) for t in state.thoughts])
                for pulse in pulses:
                    pulse_meta = dict(pulse.get("meta") or {})
                    if normalized_delivery_id:
                        pulse_meta["delivery_id"] = normalized_delivery_id
                    await conn.execute("""INSERT INTO desire_pulses(event_type,drive_key,delta,source_ref,meta,created_at)
                        VALUES($1,$2,$3,$4,$5::jsonb,$6)""",
                        str(pulse.get("event_type") or ""), pulse.get("drive_key"),
                        float(pulse.get("delta") or 0), pulse.get("source_ref"),
                        json.dumps(pulse_meta), now)
        return True

    async def log_pulse(self, event_type, drive_key, delta, source_ref, meta, now):
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""INSERT INTO desire_pulses(event_type,drive_key,delta,source_ref,meta,created_at)
                VALUES($1,$2,$3,$4,$5::jsonb,$6) RETURNING id""", event_type, drive_key, delta, source_ref, json.dumps(meta or {}), now)

    async def enqueue_pending_classification(self, item, now):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO desire_pending_classifications(
                    id,text,context,intimate_scene_open,intimate_scene_id,
                    intimate_window_minutes,current_implicit,created_at,updated_at
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$8)
                ON CONFLICT(id) DO NOTHING
            """, str(item.id), str(item.text), str(item.context),
                bool(item.intimate_scene_open), str(item.intimate_scene_id),
                int(item.intimate_window_minutes), bool(item.current_implicit), now)

    async def list_pending_classifications(self, limit=500):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id,text,context,intimate_scene_open,intimate_scene_id,
                       intimate_window_minutes,current_implicit,attempt_count,
                       next_retry_at,status
                FROM desire_pending_classifications
                ORDER BY created_at,id LIMIT $1
            """, max(1, min(5000, int(limit))))
        return [dict(row) for row in rows]

    async def mark_pending_classifications_in_flight(self, items, now):
        ids = [str(item.id) for item in items if str(item.id)]
        if not ids:
            return 0
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE desire_pending_classifications SET status='in_flight', updated_at=$2 "
                "WHERE id = ANY($1::text[])", ids, now,
            )
        return int(str(result).rsplit(" ", 1)[-1])

    async def fail_pending_classifications(self, items, reason, now):
        if not items:
            return {"failed": 0, "dead": 0}
        failed = dead = 0
        safe_reason = str(reason or "classification_failed")[:120]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for item in items:
                    item_id = str(item.id)
                    attempts = max(0, int(item.attempt_count or 0))
                    if str(item.status) == "dead":
                        await conn.execute("""
                            INSERT INTO desire_classification_dead_letters(
                                id,text,context,intimate_scene_open,intimate_scene_id,
                                intimate_window_minutes,current_implicit,attempt_count,
                                last_error,created_at,dead_at
                            )
                            SELECT id,text,context,intimate_scene_open,intimate_scene_id,
                                   intimate_window_minutes,current_implicit,$2,$3,created_at,$4
                            FROM desire_pending_classifications WHERE id=$1
                            ON CONFLICT(id) DO UPDATE SET attempt_count=EXCLUDED.attempt_count,
                                last_error=EXCLUDED.last_error,dead_at=EXCLUDED.dead_at
                        """, item_id, attempts, safe_reason, now)
                        await conn.execute(
                            "DELETE FROM desire_pending_classifications WHERE id=$1", item_id,
                        )
                        dead += 1
                    else:
                        await conn.execute("""
                            UPDATE desire_pending_classifications
                            SET status='failed', attempt_count=$2, next_retry_at=$3,
                                last_error=$4, updated_at=$5
                            WHERE id=$1
                        """, item_id, attempts, item.next_retry_at, safe_reason, now)
                        failed += 1
        return {"failed": failed, "dead": dead}

    async def delete_pending_classifications(self, item_ids):
        normalized = [str(item_id) for item_id in item_ids if str(item_id)]
        if not normalized:
            return 0
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM desire_pending_classifications WHERE id = ANY($1::text[])",
                normalized,
            )
        return int(str(result).rsplit(" ", 1)[-1])

    async def is_duplicate(self, event_type, source_ref, now) -> bool:
        if not source_ref:
            return False
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("""SELECT 1 FROM desire_pulses WHERE event_type=$1 AND source_ref=$2
                AND created_at >= $3 LIMIT 1""", event_type, source_ref, now - timedelta(minutes=5)))

    async def has_pulse(self, event_type, source_ref) -> bool:
        if not source_ref:
            return False
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT 1 FROM desire_pulses WHERE event_type=$1 AND source_ref=$2 LIMIT 1",
                event_type, source_ref,
            ))

    async def has_delivered_desire_since(self, since) -> bool:
        """A satisfy pulse is written only after cyberboss actually sends a desire message."""
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("""
                SELECT 1 FROM desire_pulses
                WHERE event_type='satisfy'
                  AND created_at > $1
                  AND COALESCE(meta->>'action','') LIKE 'voice_%'
                LIMIT 1
            """, since))

    async def drive_satisfied_since(self, drive_key, since) -> bool:
        """Cooldown is scoped to one drive, never to the whole desire system."""
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("""
                SELECT 1 FROM desire_pulses
                WHERE event_type='satisfy' AND drive_key=$1 AND created_at > $2
                LIMIT 1
            """, str(drive_key), since))

    async def has_unsettled_positive_drive(self, drive_key, now) -> bool:
        """Whether a positive contextual/body increment remains unexpressed."""
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("""
                SELECT 1 FROM desire_pulses p
                WHERE p.drive_key=$1 AND p.delta > 0
                  AND p.event_type IN ('contextual_drive','arousal_buildup')
                  AND p.created_at >= $2 - INTERVAL '12 hours'
                  AND p.created_at > COALESCE((
                      SELECT MAX(s.created_at) FROM desire_pulses s
                      WHERE s.event_type='satisfy' AND s.drive_key=$1
                  ), '-infinity'::timestamptz)
                LIMIT 1
            """, str(drive_key), now))

    async def max_event_drive_credit(self, event_id, drive_key) -> float:
        """Return the already-settled contribution for one logical event/drive."""
        if not event_id:
            return 0.0
        async with self.pool.acquire() as conn:
            value = await conn.fetchval("""
                SELECT COALESCE(MAX(
                    CASE
                      WHEN COALESCE(meta->>'event_credit_total','') ~ '^[0-9]+([.][0-9]+)?$'
                        THEN (meta->>'event_credit_total')::double precision
                      ELSE GREATEST(COALESCE(delta,0),0)
                    END
                ),0)
                FROM desire_pulses
                WHERE drive_key=$2
                  AND event_type IN ('contextual_drive','arousal_buildup')
                  AND COALESCE(meta->>'event_id','')=$1
            """, str(event_id), str(drive_key))
        return max(0.0, float(value or 0.0))

    async def recent_pulses(self, limit=20):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT event_type,drive_key,delta,source_ref,meta,created_at FROM desire_pulses ORDER BY created_at DESC LIMIT $1", limit)
        return [_row_with_decoded_meta(row) for row in rows]

    async def recent_wake_events(self, limit=240):
        """Return only the bounded audit trail for autonomous wake turns."""
        allowed = (
            "desire_wake_started", "desire_wake_action",
            "desire_wake_peek_arrived", "desire_wake_finished",
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT event_type,drive_key,delta,source_ref,meta,created_at
                FROM desire_pulses
                WHERE event_type = ANY($1::text[])
                ORDER BY created_at DESC LIMIT $2
            """, list(allowed), max(1, min(1000, int(limit))))
        return [_row_with_decoded_meta(row) for row in rows]

    async def pulse_events(self, event_types, since, limit=240):
        """Read a bounded set of durable scheduler/audit events."""
        normalized = [str(item).strip() for item in event_types if str(item).strip()]
        if not normalized:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT event_type,drive_key,delta,source_ref,meta,created_at
                FROM desire_pulses
                WHERE event_type = ANY($1::text[]) AND created_at >= $2
                ORDER BY created_at DESC LIMIT $3
            """, normalized, since, max(1, min(1000, int(limit))))
        return [_row_with_decoded_meta(row) for row in rows]

    async def count_pulses_since(self, event_type, since) -> int:
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval("""
                SELECT COUNT(*) FROM desire_pulses
                WHERE event_type=$1 AND created_at >= $2
            """, str(event_type), since))

    async def upsert_followup(self, item, now):
        event_key = str(item.get("unanswered_event_key") or "").strip()[:120]
        if not event_key:
            return None
        kind = str(item.get("unanswered_kind") or "")
        max_attempts = 2 if kind == "reminder" else 1
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO desire_followups(
                    event_key,question_text,kind,drive_key,thought_text,
                    confidence,intensity,max_attempts,next_due_at,created_at,updated_at
                )
                SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,$9,$9
                WHERE NOT EXISTS (
                    SELECT 1 FROM desire_followups old
                    WHERE old.status IN ('resolved','cancelled')
                      AND old.updated_at >= $9::timestamptz - INTERVAL '12 hours'
                      AND length(old.question_text) >= 6
                      AND length($2::text) >= 6
                      AND (
                          old.question_text ILIKE '%' || $2::text || '%'
                          OR $2::text ILIKE '%' || old.question_text || '%'
                      )
                )
                AND NOT EXISTS (
                    SELECT 1 FROM desire_followups open_item
                    WHERE open_item.status IN ('pending','deferred','awaiting_answer')
                      AND open_item.event_key <> $1
                      AND length(open_item.question_text) >= 6
                      AND length($2::text) >= 6
                      AND (
                          open_item.question_text ILIKE '%' || $2::text || '%'
                          OR $2::text ILIKE '%' || open_item.question_text || '%'
                      )
                )
                ON CONFLICT(event_key) DO UPDATE SET
                    question_text=EXCLUDED.question_text,
                    thought_text=EXCLUDED.thought_text,
                    confidence=GREATEST(desire_followups.confidence,EXCLUDED.confidence),
                    intensity=GREATEST(desire_followups.intensity,EXCLUDED.intensity),
                    updated_at=EXCLUDED.updated_at
                WHERE desire_followups.status IN ('pending','deferred','awaiting_answer')
                RETURNING *
            """, event_key, str(item.get("v_evidence") or "").strip()[:500], kind,
                str(item.get("unanswered_drive_key") or ""), str(item.get("unanswered_thought") or "").strip()[:80],
                float(item.get("unanswered_confidence", 0) or 0),
                float(item.get("unanswered_intensity", 0) or 0), max_attempts, now)
        return dict(row) if row else None

    async def list_open_followups(self, limit=12):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM desire_followups
                WHERE status IN ('pending','deferred','awaiting_answer')
                ORDER BY created_at ASC LIMIT $1
            """, int(limit))
        return [dict(row) for row in rows]

    async def next_due_followup(self, drive_key, now):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM desire_followups
                WHERE ($1='' OR drive_key=$1)
                  AND status IN ('pending','deferred','awaiting_answer')
                  AND attempts < max_attempts
                  AND next_due_at <= $2
                  AND (queued_at IS NULL OR queued_at < $2 - INTERVAL '30 minutes')
                ORDER BY next_due_at ASC, created_at ASC
                LIMIT 1
            """, drive_key, now)
        return dict(row) if row else None

    async def mark_followup_queued(self, followup_id, now):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE desire_followups SET queued_at=$2,updated_at=$2
                WHERE id=$1 AND status IN ('pending','deferred','awaiting_answer')
            """, int(followup_id), now)

    async def mark_followup_sent(self, followup_id, now):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE desire_followups SET
                    attempts=attempts+1,status='awaiting_answer',
                    last_asked_at=$2::timestamptz,queued_at=NULL,
                    next_due_at=$2::timestamptz+INTERVAL '6 hours',
                    updated_at=$2::timestamptz
                WHERE id=$1 AND status IN ('pending','deferred','awaiting_answer')
                RETURNING *
            """, int(followup_id), now)
        return dict(row) if row else None

    async def update_followup_status(self, event_key, status, now):
        if status not in {"resolved", "cancelled", "deferred"}:
            return None
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    UPDATE desire_followups SET status=$2,
                        resolved_at=CASE WHEN $2 IN ('resolved','cancelled') THEN $3 ELSE resolved_at END,
                        next_due_at=CASE WHEN $2='deferred' THEN $3+INTERVAL '6 hours' ELSE next_due_at END,
                        queued_at=NULL,updated_at=$3
                    WHERE event_key=$1 AND status IN ('pending','deferred','awaiting_answer')
                    RETURNING *
                """, str(event_key), status, now)
                if row and status in {"resolved", "cancelled"}:
                    # Follow-up thoughts may have been rewritten/merged, so the
                    # display text is not a reliable foreign key.  The creation
                    # pulse retains both the follow-up event key and the actual
                    # stored thought text. Remove by that provenance, while
                    # preserving a thought still referenced by another open
                    # follow-up.
                    await conn.execute("""
                        WITH candidates AS (
                            SELECT DISTINCT meta->>'thought' AS text
                            FROM desire_pulses
                            WHERE event_type='thought_autofeed'
                              AND source_ref='v-thought:unanswered:' || $1
                              AND COALESCE(meta->>'thought','') <> ''
                        ), protected AS (
                            SELECT DISTINCT p.meta->>'thought' AS text
                            FROM desire_pulses p
                            JOIN desire_followups f
                              ON p.source_ref='v-thought:unanswered:' || f.event_key
                            WHERE p.event_type='thought_autofeed'
                              AND f.status IN ('pending','deferred','awaiting_answer')
                              AND f.event_key <> $1
                        )
                        DELETE FROM desire_thoughts t
                        USING candidates c
                        WHERE t.text=c.text
                          AND NOT EXISTS (SELECT 1 FROM protected p WHERE p.text=t.text)
                    """, str(event_key))
        return dict(row) if row else None

    async def reset(self, now):
        await self.save(DesireState(dict(BASELINES), []), now, now)
