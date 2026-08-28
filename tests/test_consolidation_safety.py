import inspect
import unittest
from unittest.mock import AsyncMock, Mock, patch

from main import (
    CONSOLIDATE_CHUNK_SIZE,
    CONSOLIDATE_AUTO_MAX_ATTEMPTS,
    CONSOLIDATION_REPAIR_MODEL,
    CONSOLIDATION_MODEL_MAX_CONTENT_CHARS,
    CONSOLIDATION_ALIGNMENT_PROMPT,
    CONSOLIDATION_ALIGNMENT_PATCH_PROMPT,
    CONSOLIDATION_PROMPT,
    CONSOLIDATION_SAFE_MAX_CONTENT_CHARS,
    _align_consolidation_candidates,
    api_auto_consolidate,
    _consolidate_fragment_batch,
    _consolidate_fragment_chunk,
    _consolidation_state_key,
    _consolidation_events_policy_error,
    _consolidation_usable_events,
    _consolidation_overall_status,
    _consolidation_repair_focus,
    _drop_non_object_items,
    _openrouter_json_schema,
    _notify_consolidation_failure,
    _reset_forced_consolidation_day,
    _post_schema_then_legacy,
    _repair_consolidation_events,
)


class ConsolidationSafetyTest(unittest.TestCase):
    def test_default_chunk_size_keeps_dense_days_out_of_one_huge_request(self):
        self.assertLessEqual(CONSOLIDATE_CHUNK_SIZE, 20)

    def test_l2_uses_dedicated_consolidation_model(self):
        self.assertIn(
            "consolidation_model = CONSOLIDATION_MODEL",
            inspect.getsource(_consolidate_fragment_chunk),
        )
        self.assertIn(
            "model = CONSOLIDATION_MODEL",
            inspect.getsource(_align_consolidation_candidates),
        )

    def test_cross_chunk_pipeline_persists_candidates_before_final_write(self):
        source = inspect.getsource(_consolidate_fragment_batch)
        self.assertIn("candidate_only=True", source)
        self.assertIn("_load_consolidation_state", source)
        self.assertIn("_save_consolidation_state", source)
        self.assertIn("_align_consolidation_candidates", source)
        self.assertIn('cached.get("status") in {"validated", "ok"}', source)
        self.assertLess(
            source.index("_align_consolidation_candidates"),
            source.index("await commit_consolidation_events"),
        )
        self.assertIn("commit_consolidation_events", source)
        self.assertNotIn("await archive_decayed_memories", source)

    def test_retry_budget_is_initial_plus_one_and_scoped_by_day(self):
        self.assertEqual(CONSOLIDATE_AUTO_MAX_ATTEMPTS, 2)
        self.assertEqual(CONSOLIDATION_REPAIR_MODEL, "deepseek/deepseek-v3.2")
        self.assertEqual(
            _consolidation_state_key("2026-07-27"),
            "l2_consolidation_state:2026-07-27",
        )
        source = inspect.getsource(_consolidate_fragment_batch)
        self.assertIn("attempts >= CONSOLIDATE_AUTO_MAX_ATTEMPTS", source)
        self.assertIn("_notify_consolidation_failure", source)
        self.assertNotIn('"retry_paused"', source)
        self.assertNotIn('"paused"', source)
        self.assertIn('result.get("paid_attempts")', source)
        self.assertIn('aligned.get("paid_attempts")', source)


    def test_policy_failure_uses_one_targeted_repair_before_rejecting(self):
        chunk_source = inspect.getsource(_consolidate_fragment_chunk)
        align_source = inspect.getsource(_align_consolidation_candidates)
        repair_source = inspect.getsource(_repair_consolidation_events)
        self.assertIn("_repair_consolidation_events", chunk_source)
        self.assertIn("_repair_consolidation_events", align_source)
        self.assertIn("最小修补", repair_source)
        self.assertIn("_consolidation_repair_focus", repair_source)
        self.assertIn("missing_ids", repair_source)
        self.assertNotIn("fragments_text", repair_source)
        self.assertIn('CONSOLIDATION_REPAIR_MODEL', repair_source)
        self.assertIn('"reasoning": {"enabled": False}', repair_source)
        self.assertIn('getattr(client, "is_closed"', repair_source)
        self.assertIn("await repair_client.aclose()", repair_source)
        self.assertIn("only_overlong", repair_source)
        self.assertIn("只压缩下面列出的正文", repair_source)
        self.assertIn('repaired[event_index - 1]["content"] = content', repair_source)
        self.assertIn("局部压缩后仍为", repair_source)
        self.assertIn('"action":"replace"', repair_source)
        self.assertIn('"action":"add"', repair_source)
        self.assertIn("不要重输未修改事件", repair_source)
        self.assertIn("repaired[event_index - 1] = event", repair_source)
        self.assertIn("已经正确的事件保持不变", repair_source)
        self.assertIn("修补目标必须在400字以内", repair_source)
        self.assertIn("输出前自行复核", repair_source)
        self.assertIn('"paid_attempts": paid_attempts', chunk_source)
        self.assertIn('"paid_attempts": 2', align_source)

    def test_repair_focus_only_includes_problem_l1_sources(self):
        focus = _consolidation_repair_focus(
            [
                {"title": "甲", "content": "短。", "merged_ids": [1, 2]},
                {"title": "乙", "content": "长" * 551, "merged_ids": [2]},
            ],
            {1, 2, 3},
            [
                {"id": 1, "content": "一"},
                {"id": 2, "content": "二"},
                {"id": 3, "content": "三"},
            ],
        )
        self.assertEqual(focus["missing_ids"], [3])
        self.assertEqual(focus["duplicate_ids"], {2: [1, 2]})
        self.assertEqual(
            [item["id"] for item in focus["focused_sources"]], [2, 3]
        )
        self.assertEqual(focus["overlong_events"][0]["event_index"], 2)

    def test_candidate_mode_returns_without_writing_or_archiving(self):
        source = inspect.getsource(_consolidate_fragment_chunk)
        candidate_return = source.index("if candidate_only:")
        write_event = source.index("await create_event_memory")
        archive_sources = source.index("await absorb_consolidated_memories")
        self.assertLess(candidate_return, write_event)
        self.assertLess(candidate_return, archive_sources)

    def test_alignment_keeps_6000_token_cutoff_and_full_policy(self):
        source = inspect.getsource(_align_consolidation_candidates)
        self.assertIn('"max_tokens": 6000', source)
        self.assertIn('finish_reason', source)
        self.assertIn("_consolidation_events_policy_error", source)
        rendered = CONSOLIDATION_ALIGNMENT_PROMPT.format(candidates="[]")
        self.assertIn('{"title":"10字内标题"', rendered)
        patch_rendered = CONSOLIDATION_ALIGNMENT_PATCH_PROMPT.format(candidates="[]")
        self.assertIn('"merge_indexes":[8,9]', patch_rendered)
        self.assertIn("candidate_groups", source)
        # 编号对不上只弃用那一份补丁（候选保持分开、原样落库），绝不拒收整晚
        # （08-18 两次、08-19 一次都死在旧的整晚拒收上）；也绝不按并集盖章采纳
        # ——正文若真丢了事实，盖章就是家规最忌讳的静默丢失。
        self.assertIn("合并补丁改变了来源L1集合，弃用该补丁、候选保持分开", source)
        self.assertNotIn('replacement["merged_ids"] = sorted(expected_ids)', source)
        self.assertNotIn(
            '"error": "合并补丁改变了来源L1集合"', source,
        )
        self.assertIn("alignment model has no authority", source)
        self.assertIn("continue", source)
        self.assertNotIn("max(chunks) - min(chunks)", source)

    def test_l2_prompt_is_event_scoped_and_has_agreed_detail_limits(self):
        self.assertIn("L2是事件记忆", CONSOLIDATION_PROMPT)
        self.assertIn("不规定一天必须有几条事件", CONSOLIDATION_PROMPT)
        self.assertIn("单条L1本身就是完整独立事件", CONSOLIDATION_PROMPT)
        self.assertIn("普通事件以100~300字为目标", CONSOLIDATION_PROMPT)
        self.assertIn("每条content最多400字", CONSOLIDATION_PROMPT)
        self.assertIn("不逐条记录每个指令、回合", CONSOLIDATION_PROMPT)
        self.assertIn("体位变化确实构成重要阶段变化时可以简要提及", CONSOLIDATION_PROMPT)
        self.assertIn("绝不能冒充现实", CONSOLIDATION_PROMPT)
        self.assertEqual(CONSOLIDATION_MODEL_MAX_CONTENT_CHARS, 400)
        self.assertEqual(CONSOLIDATION_SAFE_MAX_CONTENT_CHARS, 550)

    def test_l2_strict_policy_still_drives_repair_before_permissive_terminal(self):
        good = [
            {"title": "事件一", "content": "起因经过结果。", "merged_ids": [1, 2]},
            {"title": "事件二", "content": "另一件事。", "merged_ids": [3]},
        ]
        self.assertEqual(_consolidation_events_policy_error(good, {1, 2, 3}), "")
        self.assertIn(
            "超过程序安全上限550字",
            _consolidation_events_policy_error(
                [{"title": "长事件", "content": "我" * 551, "merged_ids": [1, 2]}],
                {1, 2},
            ),
        )
        self.assertIn(
            "未被任何事件覆盖",
            _consolidation_events_policy_error(
                [{"title": "漏项", "content": "内容。", "merged_ids": [1]}],
                {1, 2},
            ),
        )
        self.assertIn(
            "重复分配",
            _consolidation_events_policy_error(
                [
                    {"title": "甲", "content": "内容。", "merged_ids": [1]},
                    {"title": "乙", "content": "内容。", "merged_ids": [1, 2]},
                ],
                {1, 2},
            ),
        )
        self.assertEqual(
            _consolidation_events_policy_error(
                [
                    {"title": "甲", "content": "内容。", "merged_ids": [1]},
                    {"title": "乙", "content": "内容。", "merged_ids": [1, 2]},
                ],
                {1, 2},
                allow_duplicate_ids=True,
            ),
            "",
        )
        usable, covered, missing = _consolidation_usable_events(
            [
                {"title": "长事件", "content": "我" * 551, "merged_ids": [1]},
                {"title": "重复来源", "content": "仍是合法事件。", "merged_ids": [1]},
            ],
            {1, 2},
            "test",
        )
        self.assertEqual(len(usable), 2)
        self.assertEqual(covered, {1})
        self.assertEqual(missing, {2})

    def test_full_malformed_json_spends_one_clean_regeneration(self):
        source = inspect.getsource(_consolidate_fragment_chunk)
        self.assertIn("L2整块JSON解析失败", source)
        self.assertIn('"max_tokens": 6000', source)
        self.assertIn("paid_attempts += 1", source)
        self.assertIn("整块重生成JSON解析失败", source)
        self.assertIn("整块JSON解析失败且付费额度已用尽", source)

    def test_non_object_items_are_dropped_at_the_parse_boundary(self):
        # 2026-08-18 nightly L2: a bare int in the model array reached
        # _consolidation_repair_focus and killed chunk 0 with
        # "'int' object has no attribute 'get'".
        good = {"title": "甲", "content": "正文。", "merged_ids": [1]}
        self.assertEqual(_drop_non_object_items([good, 7], "test"), [good])
        self.assertEqual(_drop_non_object_items([0, "x", None], "test"), None)
        self.assertIsNone(_drop_non_object_items(None, "test"))
        self.assertIsNone(_drop_non_object_items([], "test"))

        chunk_source = inspect.getsource(_consolidate_fragment_chunk)
        align_source = inspect.getsource(_align_consolidation_candidates)
        self.assertEqual(chunk_source.count("_drop_non_object_items"), 2)
        self.assertIn("_drop_non_object_items", align_source)
        self.assertIn("patch_mode and parsed_output == []", align_source)

    def test_repair_focus_failure_costs_an_attempt_not_the_chunk(self):
        source = inspect.getsource(_repair_consolidation_events)
        focus_at = source.index("_consolidation_repair_focus")
        self.assertLess(source.index("try:"), focus_at)
        self.assertIn("定点修补取材异常", source)
        self.assertEqual(
            _consolidation_repair_focus([{"merged_ids": [1]}], {1}, [])["missing_ids"],
            [],
        )

    def test_forced_retry_keeps_one_alignment_try_and_still_converges(self):
        source = inspect.getsource(_reset_forced_consolidation_day)
        self.assertIn(
            'state["align_attempts"] = max(0, CONSOLIDATE_AUTO_MAX_ATTEMPTS - 1)',
            source,
        )
        self.assertNotIn('state["align_attempts"] = 0', source)
        # One forced run must be able to reach the fallback that commits the
        # already-validated chunk candidates instead of looping forever.
        self.assertGreaterEqual(
            max(0, CONSOLIDATE_AUTO_MAX_ATTEMPTS - 1) + 1,
            CONSOLIDATE_AUTO_MAX_ATTEMPTS,
        )

    def test_successful_alignment_fallback_does_not_send_a_false_failure_alert(self):
        batch_source = inspect.getsource(_consolidate_fragment_batch)
        fallback_at = batch_source.index('state["align_status"] = "fallback" if final')
        nonfinal_at = batch_source.index("if not final:", fallback_at)
        notify_at = batch_source.index("_notify_consolidation_failure", nonfinal_at)
        direct_fallback_at = batch_source.index("final_events = candidates", notify_at)
        self.assertLess(nonfinal_at, notify_at)
        self.assertLess(notify_at, direct_fallback_at)
        alert_source = inspect.getsource(_notify_consolidation_failure)
        self.assertNotIn("源碎片保持未归档", alert_source)
        self.assertIn("原始L1记录未被本步骤删除", alert_source)

    def test_outer_status_reports_nested_failures(self):
        self.assertEqual(_consolidation_overall_status([]), "ok")
        self.assertEqual(_consolidation_overall_status([{"status": "ok"}]), "ok")
        self.assertEqual(_consolidation_overall_status([{"status": "error"}]), "partial_error")
        self.assertEqual(_consolidation_overall_status([{"status": "partial"}]), "partial_error")

    def test_uncovered_fragments_make_chunk_partial(self):
        source = inspect.getsource(_consolidate_fragment_chunk)
        self.assertIn('"status": "ok" if uncovered_count == 0 else "partial"', source)

    def test_auto_job_marks_running_before_scheduling_background_work(self):
        source = inspect.getsource(api_auto_consolidate)
        update_at = source.index('_consolidate_status.update({"running": True')
        task_at = source.index("asyncio.create_task(_run())")
        self.assertLess(update_at, task_at)


class StructuredOutputFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_patch_alignment_accepts_preexisting_cross_chunk_duplicate_ids(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "[]"},
            }]
        }
        client = Mock()
        client.post = AsyncMock(return_value=response)
        candidates = [
            {"title": "甲", "content": "第一件事。", "importance": 5, "merged_ids": [1]},
            {"title": "乙", "content": "另一件事。", "importance": 5, "merged_ids": [1, 2]},
        ]
        with patch("main.httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__ = AsyncMock(return_value=client)
            client_class.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await _align_consolidation_candidates(
                candidates,
                {1, 2},
                candidate_groups=[[candidates[0]], [candidates[1]]],
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["events"], candidates)
        self.assertEqual(result["paid_attempts"], 1)
        self.assertEqual(client.post.await_count, 1)

    async def test_schema_http_400_falls_back_to_legacy_request(self):
        schema_response = Mock(status_code=400)
        legacy_response = Mock(status_code=200)
        client = Mock()
        client.post = AsyncMock(side_effect=[schema_response, legacy_response])
        schema = {
            "type": "object",
            "properties": {"patches": {"type": "array", "items": {}}},
            "required": ["patches"],
            "additionalProperties": False,
        }
        with patch("main.API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"):
            response, used_schema = await _post_schema_then_legacy(
                client,
                headers={"Authorization": "redacted"},
                stage="test",
                structured_payload={
                    "response_format": _openrouter_json_schema("test", schema)
                },
                legacy_payload={"messages": []},
            )
        self.assertIs(response, legacy_response)
        self.assertFalse(used_schema)
        self.assertEqual(client.post.await_count, 2)
        self.assertIn(
            "response_format",
            client.post.await_args_list[0].kwargs["json"],
        )
        self.assertNotIn(
            "response_format",
            client.post.await_args_list[1].kwargs["json"],
        )

    async def test_schema_exception_falls_back_to_legacy_request(self):
        legacy_response = Mock(status_code=200)
        client = Mock()
        client.post = AsyncMock(
            side_effect=[RuntimeError("schema unavailable"), legacy_response]
        )
        with patch("main.API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"):
            response, used_schema = await _post_schema_then_legacy(
                client,
                headers={},
                stage="test",
                structured_payload={"response_format": {}},
                legacy_payload={"messages": []},
            )
        self.assertIs(response, legacy_response)
        self.assertFalse(used_schema)
        self.assertEqual(client.post.await_count, 2)

    async def test_l2_repair_schema_400_returns_legacy_repaired_events(self):
        schema_response = Mock(status_code=400)
        legacy_response = Mock(status_code=200)
        legacy_response.json.return_value = {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": (
                        '[{"action":"replace","event_index":2,'
                        '"event":{"title":"乙","content":"另一件事。",'
                        '"importance":5,"merged_ids":[2]}}]'
                    )
                },
            }]
        }
        client = Mock(is_closed=False)
        client.post = AsyncMock(side_effect=[schema_response, legacy_response])
        events = [
            {"title": "甲", "content": "第一件事。", "importance": 5, "merged_ids": [1]},
            {"title": "乙", "content": "另一件事。", "importance": 5, "merged_ids": [1, 2]},
        ]
        with patch("main.API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"):
            result = await _repair_consolidation_events(
                client,
                "anthropic/claude-haiku-4.5",
                events,
                [{"id": 1, "content": "一"}, {"id": 2, "content": "二"}],
                {1, 2},
                "L1 ID被重复分配",
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["events"][1]["merged_ids"], [2])
        self.assertEqual(client.post.await_count, 2)


if __name__ == "__main__":
    unittest.main()
