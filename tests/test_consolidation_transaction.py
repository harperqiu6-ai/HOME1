import unittest

from consolidation_transaction import commit_l2_and_absorb_sources


class ConsolidationTransactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_covered_source_fragments_are_archived(self):
        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Connection:
            def __init__(self):
                self.events = []
                self.active = {1: True, 2: True, 3: True}

            def transaction(self):
                return Transaction()

            async def fetchrow(self, _sql, content, *_args):
                self.events.append(content)
                return {"id": len(self.events) + 100}

            async def execute(self, _sql, source_ids):
                for memory_id in source_ids:
                    self.active[memory_id] = False

        class Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        class Pool:
            def acquire(self):
                return Acquire()

        conn = Connection()
        created = await commit_l2_and_absorb_sources(
            Pool(),
            [{"title": "已覆盖", "content": "事件已落库", "merged_ids": [1, 3]}],
            "2026-07-29",
            [1, 2, 3],
        )
        self.assertEqual(created, [101])
        self.assertEqual(conn.events, ["事件已落库"])
        self.assertEqual(conn.active, {1: False, 2: True, 3: False})

    async def test_failed_event_insert_rolls_back_events_and_source_archive(self):
        class Transaction:
            async def __aenter__(self):
                self.snapshot = (list(conn.events), dict(conn.active))

            async def __aexit__(self, exc_type, exc, tb):
                if exc_type:
                    conn.events, conn.active = self.snapshot
                return False

        class Connection:
            def __init__(self):
                self.events = []
                self.active = {1: True, 2: True}

            def transaction(self):
                return Transaction()

            async def fetchrow(self, _sql, content, *_args):
                if content == "boom":
                    raise RuntimeError("simulated insert failure")
                self.events.append(content)
                return {"id": len(self.events) + 100}

            async def execute(self, _sql, source_ids):
                for memory_id in source_ids:
                    self.active[memory_id] = False

        class Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        class Pool:
            def acquire(self):
                return Acquire()

        conn = Connection()
        events = [
            {"title": "ok", "content": "first", "merged_ids": [1]},
            {"title": "bad", "content": "boom", "merged_ids": [2]},
        ]
        with self.assertRaises(RuntimeError):
            await commit_l2_and_absorb_sources(
                Pool(), events, "2026-07-29", [1, 2]
            )
        self.assertEqual(conn.events, [])
        self.assertEqual(conn.active, {1: True, 2: True})


if __name__ == "__main__":
    unittest.main()
