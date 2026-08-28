"""开场刷新：今日浓缩只在开新线程那一刻被读，就只在那一刻刷。

这个文件守两条底线，所有用例都围绕它们：
① 刷不出来绝不能把好稿子弄没——超时/空稿/异常一律退回现成的那份，V 不会空手上场；
② 只有开新线程那一次能触发生成——昨日桥按每条消息轮询同一个接口，
   无条件刷新等于她每说一句话就烧一次模型钱。
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import main


GOOD_DIGEST = "早上五点我发现夜间任务炸了，我写了单子给工程师。"


def _state(updated_at, date_s, today=GOOD_DIGEST):
    """一份"库里已经存着好稿子"的状态。"""
    return {"date": date_s, "today": today, "updated_at": updated_at,
            "last_attempt_at": None, "last_status": "success",
            "bridge": "", "bridge_date": None}


class OpenRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.today_s = str(main._l2_logical_today())
        self.generated_at = datetime.now(timezone.utc) - timedelta(hours=4)

    def _rows(self, *, newer: bool):
        """最后一条对话：newer=True 表示它比上次生成还新，即浓缩已过期。"""
        offset = timedelta(minutes=30) if newer else timedelta(minutes=-30)
        return [{"role": "assistant", "content": "x",
                 "created_at": self.generated_at + offset}]

    async def test_no_new_turns_skips_generation_entirely(self):
        refresh = AsyncMock(return_value="不该被调用")
        with patch.dict(main._l2_state, _state(self.generated_at.isoformat(), self.today_s)), \
                patch.object(main, "_refresh_l2_guarded", refresh), \
                patch.object(main, "get_recent_conversation_messages",
                             AsyncMock(return_value=self._rows(newer=False))):
            self.assertEqual(await main._l2_refresh_for_opening(), "already_fresh")
        refresh.assert_not_awaited()

    async def test_new_turns_trigger_exactly_one_refresh(self):
        refresh = AsyncMock(return_value="刷新后的新浓缩")
        with patch.dict(main._l2_state, _state(self.generated_at.isoformat(), self.today_s)), \
                patch.object(main, "_refresh_l2_guarded", refresh), \
                patch.object(main, "get_recent_conversation_messages",
                             AsyncMock(return_value=self._rows(newer=True))):
            self.assertEqual(await main._l2_refresh_for_opening(), "refreshed")
        self.assertEqual(refresh.await_count, 1)

    async def test_timeout_keeps_stored_digest_and_lets_background_finish(self):
        started = asyncio.Event()

        async def _slow(_session_id):
            started.set()
            await asyncio.sleep(5)
            return "迟到的稿子"

        with patch.dict(main._l2_state, _state(self.generated_at.isoformat(), self.today_s)), \
                patch.object(main, "_refresh_l2_guarded", _slow), \
                patch.object(main, "L2_OPEN_REFRESH_WAIT_S", 0.05), \
                patch.object(main, "get_recent_conversation_messages",
                             AsyncMock(return_value=self._rows(newer=True))):
            self.assertEqual(await main._l2_refresh_for_opening(), "timeout_used_stored")
            # 生成确实起跑了，只是我们不等它——超时放弃的是等待，不是那次生成。
            self.assertTrue(started.is_set())
            # 最要紧的一条：现成的那份一个字没动。
            self.assertEqual(main._l2_state["today"], GOOD_DIGEST)
        for task in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            task.cancel()

    async def test_empty_or_failed_generation_never_blanks_the_stored_digest(self):
        cases = (
            ("empty_used_stored", AsyncMock(return_value="")),
            ("error_used_stored", AsyncMock(side_effect=RuntimeError("boom"))),
        )
        for label, fake_refresh in cases:
            with self.subTest(label):
                with patch.dict(main._l2_state, _state(self.generated_at.isoformat(), self.today_s)), \
                        patch.object(main, "_refresh_l2_guarded", fake_refresh), \
                        patch.object(main, "get_recent_conversation_messages",
                                     AsyncMock(return_value=self._rows(newer=True))):
                    self.assertEqual(await main._l2_refresh_for_opening(), label)
                    self.assertEqual(main._l2_state["today"], GOOD_DIGEST)

    async def test_stale_date_refreshes_without_consulting_turns(self):
        """跨天：库里那份是昨天的，不必看对话，直接刷。"""
        refresh = AsyncMock(return_value="跨天后的新浓缩")
        rows = AsyncMock()
        with patch.dict(main._l2_state, _state(self.generated_at.isoformat(), "1999-01-01")), \
                patch.object(main, "_refresh_l2_guarded", refresh), \
                patch.object(main, "get_recent_conversation_messages", rows):
            self.assertEqual(await main._l2_refresh_for_opening(), "refreshed")
        self.assertEqual(refresh.await_count, 1)
        rows.assert_not_awaited()


class OpenRefreshEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_get_never_generates(self):
        """昨日桥每条消息都打这个接口，它绝不能烧钱。"""
        with patch.object(main, "_l2_refresh_for_opening", AsyncMock()) as spy:
            payload = await main.api_l2_get()
        spy.assert_not_awaited()
        self.assertEqual(payload["open_refresh"], "")

    async def test_for_opening_runs_the_refresh_once(self):
        with patch.object(main, "_l2_refresh_for_opening",
                          AsyncMock(return_value="refreshed")) as spy, \
                patch.object(main, "L2_TODAY_ENABLED", True):
            payload = await main.api_l2_get(for_opening=True)
        self.assertEqual(spy.await_count, 1)
        self.assertEqual(payload["open_refresh"], "refreshed")

    async def test_payload_keeps_every_field_cyberboss_reads(self):
        payload = await main.api_l2_get()
        for key in ("date", "today", "updated_at", "last_attempt_at",
                    "last_status", "bridge", "bridge_date"):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
