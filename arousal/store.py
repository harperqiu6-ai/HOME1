"""PostgreSQL persistence adapter for arousal v1.

本系统只用于所有参与者均为成年人的、自愿的虚构亲密互动；停止、否定与控制信号永远优先于刺激识别。
Only for consensual fictional intimate interaction between adults. Stop,
negation, and control signals always take priority over stimulus recognition.

This intentionally differs from the tutorial's JSON + SQLite example. HOME1
already has an asyncpg pool and PostgreSQL-backed desire state; keeping body
state and its permanent event ledger in one transaction makes them one
authoritative backup unit and remains correct across multiple workers.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Awaitable, Callable

from database import get_pool

from .core import initial_state


def event_digest(event_id: str) -> str:
    return hashlib.sha256(str(event_id).encode("utf-8")).hexdigest()


class ArousalStore:
    async def transact(self, *, event_id: str, kind: str, now: float,
                       apply: Callable[[dict], tuple[dict, object] | Awaitable[tuple[dict, object]]]):
        """Lock, deduplicate, apply, and persist one canonical event atomically."""
        digest = event_digest(event_id)
        pool = await get_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                if await connection.fetchval("SELECT 1 FROM arousal_events WHERE digest=$1", digest):
                    row = await connection.fetchrow("SELECT payload FROM arousal_state WHERE id=1 FOR UPDATE")
                    return self._decode(row, now), None, True
                row = await connection.fetchrow("SELECT payload FROM arousal_state WHERE id=1 FOR UPDATE")
                if row is None:
                    initialized = await connection.fetchval("SELECT EXISTS(SELECT 1 FROM arousal_events)")
                    state = self._quarantined(now) if initialized else initial_state(now)
                else:
                    state = self._decode(row, now)
                result = apply(state)
                if hasattr(result, "__await__"):
                    result = await result
                new_state, value = result
                await connection.execute(
                    """
                    INSERT INTO arousal_state(id,payload,updated_at) VALUES(1,$1::jsonb,NOW())
                    ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()
                    """,
                    json.dumps(new_state, ensure_ascii=False, allow_nan=False),
                )
                receipt = new_state.get("pending_release_effect")
                if isinstance(receipt, dict):
                    await connection.execute(
                        """
                        INSERT INTO arousal_release_effects(effect_id,payload,targets_done,created_at)
                        VALUES($1,$2::jsonb,$3::jsonb,$4)
                        ON CONFLICT(effect_id) DO NOTHING
                        """,
                        receipt["effect_id"],
                        json.dumps(receipt, ensure_ascii=False, allow_nan=False),
                        json.dumps(receipt["targets"], allow_nan=False),
                        datetime.fromtimestamp(float(receipt["created_at"]), timezone.utc),
                    )
                await connection.execute(
                    "INSERT INTO arousal_events(digest,kind,created_at) VALUES($1,$2,$3)",
                    digest, kind, datetime.fromtimestamp(float(now), timezone.utc),
                )
                return new_state, value, False

    async def read(self, now: float) -> dict:
        pool = await get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow("SELECT payload FROM arousal_state WHERE id=1")
            return initial_state(now) if row is None else self._decode(row, now)

    async def save_effect_state(self, state: dict, now: float) -> None:
        """Persist receipt acknowledgements while serializing with event writers."""
        pool = await get_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.fetchrow("SELECT payload FROM arousal_state WHERE id=1 FOR UPDATE")
                receipt = state.get("pending_release_effect")
                if receipt:
                    await connection.execute(
                        """
                        INSERT INTO arousal_release_effects(effect_id,payload,targets_done,created_at)
                        VALUES($1,$2::jsonb,$3::jsonb,$4)
                        ON CONFLICT(effect_id) DO UPDATE
                        SET payload=EXCLUDED.payload,targets_done=EXCLUDED.targets_done
                        """,
                        receipt["effect_id"],
                        json.dumps(receipt, ensure_ascii=False, allow_nan=False),
                        json.dumps(receipt["targets"], allow_nan=False),
                        datetime.fromtimestamp(float(receipt["created_at"]), timezone.utc),
                    )
                await connection.execute(
                    "UPDATE arousal_state SET payload=$1::jsonb,updated_at=NOW() WHERE id=1",
                    json.dumps(state, ensure_ascii=False, allow_nan=False),
                )

    async def ack_effect(self, *, effect_id: str, target: str, now: float) -> dict:
        """Atomically ack one target against the latest locked state."""
        from .core import ack_release_effect

        pool = await get_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow("SELECT payload FROM arousal_state WHERE id=1 FOR UPDATE")
                state = self._decode(row, now)
                state = ack_release_effect(state, effect_id=effect_id, target=target, now=now)
                receipt = state.get("pending_release_effect")
                targets = receipt.get("targets", {}) if isinstance(receipt, dict) else {"somatic": True, "drive": True}
                await connection.execute(
                    "UPDATE arousal_state SET payload=$1::jsonb,updated_at=NOW() WHERE id=1",
                    json.dumps(state, ensure_ascii=False, allow_nan=False),
                )
                await connection.execute(
                    """
                    UPDATE arousal_release_effects
                    SET targets_done=$2::jsonb,
                        completed_at=CASE WHEN $3 THEN NOW() ELSE completed_at END
                    WHERE effect_id=$1
                    """,
                    effect_id, json.dumps(targets), receipt is None,
                )
                return state

    @staticmethod
    def _decode(row, now: float) -> dict:
        try:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict) or not payload:
                raise ValueError("empty arousal state")
            return payload
        except Exception:
            return ArousalStore._quarantined(now)

    @staticmethod
    def _quarantined(now: float) -> dict:
        state = initial_state(now)
        state["release_gate"]["locked"] = True
        state["quarantined"] = True
        return state
