"""Small dependency-free transaction boundary for L2 consolidation."""


async def commit_l2_and_absorb_sources(pool, events: list, event_date,
                                       source_ids: list) -> list:
    """Insert all L2 rows and absorb only sources actually covered by them."""
    covered_ids = {
        int(value)
        for event in events
        for value in event.get("merged_ids", [])
    }
    allowed_ids = {int(value) for value in source_ids}
    source_ids = sorted(covered_ids & allowed_ids)
    created_ids = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for event in events:
                row = await conn.fetchrow("""
                    INSERT INTO memories
                        (content, importance, layer, title, is_active,
                         merged_from, event_date)
                    VALUES ($1, $2, 2, $3, TRUE, $4, $5)
                    RETURNING id
                """, event.get("content", ""), event.get("importance", 5),
                    event.get("title", ""),
                    [int(value) for value in event.get("merged_ids", [])],
                    event_date)
                if not row:
                    raise RuntimeError("L2 event insert returned no id")
                created_ids.append(int(row["id"]))
            await conn.execute("""
                UPDATE memories SET is_active = FALSE, decayed_at = NULL
                WHERE id = ANY($1::int[])
                AND kind <> 'musing'
            """, [int(value) for value in source_ids])
    return created_ids
