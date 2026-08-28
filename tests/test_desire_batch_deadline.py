import asyncio
import unittest

from desire_pulse import DeepSeekBatcher, PendingClassification


class DesireBatchDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_hard_deadline_requeues_items_instead_of_holding_flush_forever(self):
        failures = []

        async def on_results(_results):
            return None

        async def on_failed(items, reason):
            failures.append(([item.id for item in items], reason))

        batcher = DeepSeekBatcher(
            on_results,
            api_key="test",
            batch_size=20,
            request_deadline_seconds=1,
            retry_base_seconds=1,
            on_batch_failed=on_failed,
        )

        async def never_returns(_items):
            await asyncio.Future()

        batcher._request = never_returns
        await batcher.enqueue_item(
            PendingClassification(id="deadline-1", text="普通语境"),
            allow_immediate_flush=False,
        )
        accepted = await batcher.flush()

        self.assertEqual(accepted, [])
        self.assertEqual(failures, [(["deadline-1"], "TimeoutError")])
        self.assertEqual(len(batcher.items), 1)
        self.assertEqual(batcher.items[0].status, "failed")
        self.assertEqual(batcher.items[0].attempt_count, 1)
        self.assertIsNotNone(batcher.items[0].next_retry_at)


if __name__ == "__main__":
    unittest.main()
