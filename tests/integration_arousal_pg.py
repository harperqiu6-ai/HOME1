#!/usr/bin/env python3
"""Live PostgreSQL integration checks for arousal v1.

Run only as the ``home1`` OS user. The script reads DATABASE_URL from the live
environment file, touches only the three ``arousal_*`` tables, and restores
their exact starting rows in ``finally``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
import uuid
from pathlib import Path


TABLES = ("arousal_state", "arousal_events", "arousal_release_effects")
ENV_PATH = Path("/opt/home1/home1.env")


def _load_database_url() -> None:
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if separator and key.strip() == "DATABASE_URL":
            os.environ["DATABASE_URL"] = value.strip().strip("\"'")
            return
    raise RuntimeError("DATABASE_URL not found in live environment")


_load_database_url()

from arousal.core import (  # noqa: E402
    apply_assistant_event,
    apply_user_event,
    initial_state,
)
from arousal.store import ArousalStore  # noqa: E402
from database import close_pool, get_pool  # noqa: E402


LEXICON = {
    "touch": [
        {"kw": "按动", "delta": .8},
        {"kw": "摩擦", "delta": .6},
    ],
    "body_parts": {"敏感点": {"sensitivity": .95}},
}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_bind(value) -> str:
    return value if isinstance(value, str) else _json(value)


def _json_value(value):
    return json.loads(value) if isinstance(value, str) else value


async def _counts(connection) -> dict[str, int]:
    return {
        table: int(await connection.fetchval(f"SELECT count(*) FROM {table}"))
        for table in TABLES
    }


async def _snapshot(connection) -> dict[str, list[dict]]:
    return {
        "arousal_state": [
            dict(row) for row in await connection.fetch(
                "SELECT id,payload,updated_at FROM arousal_state ORDER BY id"
            )
        ],
        "arousal_events": [
            dict(row) for row in await connection.fetch(
                "SELECT digest,kind,created_at FROM arousal_events ORDER BY digest"
            )
        ],
        "arousal_release_effects": [
            dict(row) for row in await connection.fetch(
                """
                SELECT effect_id,payload,targets_done,created_at,completed_at
                FROM arousal_release_effects ORDER BY effect_id
                """
            )
        ],
    }


async def _restore(connection, snapshot: dict[str, list[dict]]) -> None:
    async with connection.transaction():
        await connection.execute("DELETE FROM arousal_release_effects")
        await connection.execute("DELETE FROM arousal_events")
        await connection.execute("DELETE FROM arousal_state")
        for row in snapshot["arousal_state"]:
            await connection.execute(
                "INSERT INTO arousal_state(id,payload,updated_at) VALUES($1,$2::jsonb,$3)",
                row["id"], _json_bind(row["payload"]), row["updated_at"],
            )
        for row in snapshot["arousal_events"]:
            await connection.execute(
                "INSERT INTO arousal_events(digest,kind,created_at) VALUES($1,$2,$3)",
                row["digest"], row["kind"], row["created_at"],
            )
        for row in snapshot["arousal_release_effects"]:
            await connection.execute(
                """
                INSERT INTO arousal_release_effects(
                    effect_id,payload,targets_done,created_at,completed_at
                ) VALUES($1,$2::jsonb,$3::jsonb,$4,$5)
                """,
                row["effect_id"], _json_bind(row["payload"]), _json_bind(row["targets_done"]),
                row["created_at"], row["completed_at"],
            )


async def _prepare_pending(store: ArousalStore, prefix: str, now: float) -> tuple[dict, str]:
    state = await store.read(now)
    for index in range(1, 20):
        event_id = f"{prefix}:user:{index}"
        state, _, duplicate = await store.transact(
            event_id=event_id,
            kind="user",
            now=now + index,
            apply=lambda current, eid=event_id, tick=now + index: apply_user_event(
                current,
                "按动并摩擦敏感点",
                event_id=eid,
                libido=.4,
                now=tick,
                lexicon=LEXICON,
            ),
        )
        assert not duplicate
        if state.get("pending_release"):
            return state, event_id
    raise AssertionError("failed to reach pending release")


async def _release(store: ArousalStore, prefix: str, now: float) -> tuple[dict, str]:
    state, parent = await _prepare_pending(store, prefix, now)
    assistant_id = f"{prefix}:assistant:final"
    state, fired, duplicate = await store.transact(
        event_id=assistant_id,
        kind="assistant",
        now=now + 30,
        apply=lambda current: apply_assistant_event(
            current,
            "",
            event_id=assistant_id,
            source_user_event_id=parent,
            complete=True,
            libido=.4,
            now=now + 30,
        ),
    )
    assert fired and not duplicate
    return state, assistant_id


async def main() -> None:
    if os.geteuid() == 0:
        raise RuntimeError("must run as home1, not root")
    pool = await get_pool()
    async with pool.acquire() as connection:
        before = await _counts(connection)
        snapshot = await _snapshot(connection)
    print("before:", _json(before))

    store = ArousalStore()
    prefix = f"arousal-pg-{uuid.uuid4().hex}"
    base = time.time()
    checks: list[str] = []
    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("DELETE FROM arousal_release_effects")
                await connection.execute("DELETE FROM arousal_events")
                await connection.execute("DELETE FROM arousal_state")

        # a. Permanent event ledger replay is duplicate and JSONB bytes do not move.
        event_id = f"{prefix}:replay:user"
        _, _, duplicate = await store.transact(
            event_id=event_id,
            kind="user",
            now=base,
            apply=lambda current: apply_user_event(
                current, "按动敏感点", event_id=event_id, libido=.4,
                now=base, lexicon=LEXICON,
            ),
        )
        assert not duplicate
        async with pool.acquire() as connection:
            payload_before = await connection.fetchval(
                "SELECT payload::text FROM arousal_state WHERE id=1"
            )
        _, _, duplicate = await store.transact(
            event_id=event_id,
            kind="user",
            now=base + 999,
            apply=lambda current: (_ for _ in ()).throw(
                AssertionError("duplicate apply callback ran")
            ),
        )
        async with pool.acquire() as connection:
            payload_after = await connection.fetchval(
                "SELECT payload::text FROM arousal_state WHERE id=1"
            )
        assert duplicate is True and payload_after == payload_before
        checks.append("a replay duplicate + payload byte-stable")

        # b. Replayed assistant final cannot release twice.
        released, assistant_id = await _release(store, f"{prefix}:b", base + 1000)
        receipt_before = copy.deepcopy(released["pending_release_effect"])
        replayed, fired, duplicate = await store.transact(
            event_id=assistant_id,
            kind="assistant",
            now=base + 5000,
            apply=lambda current: (_ for _ in ()).throw(
                AssertionError("assistant duplicate apply callback ran")
            ),
        )
        assert duplicate is True and fired is None
        assert replayed["pending_release_effect"] == receipt_before
        checks.append("b assistant final replay did not release twice")

        # c. Separate target acks complete the durable row and state tombstone.
        effect_id = receipt_before["effect_id"]
        one = await store.ack_effect(effect_id=effect_id, target="somatic", now=base + 1040)
        assert one["pending_release_effect"]["targets"] == {"somatic": True, "drive": False}
        done = await store.ack_effect(effect_id=effect_id, target="drive", now=base + 1041)
        assert done["pending_release_effect"] is None
        assert effect_id in done["completed_release_effect_ids"]
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT targets_done,completed_at
                FROM arousal_release_effects WHERE effect_id=$1
                """,
                effect_id,
            )
        assert _json_value(row["targets_done"]) == {"somatic": True, "drive": True}
        assert row["completed_at"] is not None
        checks.append("c somatic/drive ack completed row + tombstone + cleared receipt")

        # d. Simulate a crash after the first ack; rerun delivers only the missing target.
        seed_id = f"{prefix}:d:seed"
        seed = initial_state(base + 2000)
        seed["value"] = .97
        seed["pending_release"] = {
            "source_user_event_id": f"{prefix}:d:parent",
            "created_at": base + 2000,
        }
        await store.transact(
            event_id=seed_id,
            kind="test_seed",
            now=base + 2000,
            apply=lambda _: (seed, None),
        )
        final_id = f"{prefix}:d:assistant"
        crashed, fired, duplicate = await store.transact(
            event_id=final_id,
            kind="assistant",
            now=base + 2001,
            apply=lambda current: apply_assistant_event(
                current, "", event_id=final_id,
                source_user_event_id=f"{prefix}:d:parent", complete=True,
                libido=.4, now=base + 2001,
            ),
        )
        assert fired and not duplicate
        crash_effect_id = crashed["pending_release_effect"]["effect_id"]
        impacts = {"somatic": 0, "drive": 0}

        impacts["somatic"] += 1
        await store.ack_effect(
            effect_id=crash_effect_id, target="somatic", now=base + 2002
        )
        for target in ("somatic", "drive"):
            current = await store.read(base + 2003)
            receipt = current.get("pending_release_effect")
            if receipt and not receipt["targets"].get(target):
                impacts[target] += 1
                await store.ack_effect(
                    effect_id=crash_effect_id, target=target, now=base + 2003
                )
        for target in ("somatic", "drive"):
            current = await store.read(base + 2004)
            receipt = current.get("pending_release_effect")
            if receipt and not receipt["targets"].get(target):
                impacts[target] += 1
        assert impacts == {"somatic": 1, "drive": 1}
        checks.append("d half-ack crash replay affected each downstream once")

        for check in checks:
            print("PASS", check)
    finally:
        async with pool.acquire() as connection:
            await _restore(connection, snapshot)
            after = await _counts(connection)
        print("after: ", _json(after))
        print("cleanup:", "CLEAN" if after == before else "MISMATCH")
        await close_pool()
    assert after == before
    print(f"=== {len(checks)} integration checks passed ===")


if __name__ == "__main__":
    asyncio.run(main())
