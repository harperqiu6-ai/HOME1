import unittest
import inspect
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from main import (_compact_daily_diary_body, _daily_diary_body_is_valid,
                  _daily_diary_coverage_error, _parse_daily_diary_payload,
                  _daily_diary_marker_result,
                  _daily_diary_body_result,
                  _generate_daily_diary_chunked,
                  _daily_diary_summary_is_valid, _generate_daily_diary_summary,
                  _compose_mw_content, _parse_daily_diary_sections,
                  _repair_missing_daily_diary_summary,
                  _generate_daily_diary_card,
                  generate_daily_diary,
                  maybe_run_dreams,
                  process_memories_background,
                  _recent_missing_daily_diary_targets,
                  _format_daily_diary_events, _format_daily_diary_fragments, _format_l2_conversation,
                  _l2_digest_needs_compaction, _scrub_digest_explicit,
                  group_by_rounds, _memorywall_summary_fallback,
                  _memorywall_summary_is_valid, _summary_word_count,
                  api_mw_update)
from database import (get_daily_diary_events, get_daily_diary_fragments, get_fragment_ids_for_date, get_memorywall_dates,
                      get_memorywall_summary_by_date)


class DailyDiarySummaryTest(unittest.TestCase):
    def test_automatic_diary_targets_only_include_recent_logical_days(self):
        targets = _recent_missing_daily_diary_targets(
            ["2026-06-20", "2026-07-22", "2026-07-23", "2026-07-24", "2026-07-25", "2026-07-26"],
            {"2026-07-24"},
            date(2026, 7, 26),
            lookback_days=3,
        )
        self.assertEqual(targets, ["2026-07-23", "2026-07-25"])

    def test_automatic_diary_targets_never_include_today_or_future(self):
        targets = _recent_missing_daily_diary_targets(
            ["2026-07-25", "2026-07-26", "2026-07-27"],
            set(),
            date(2026, 7, 26),
            lookback_days=3,
        )
        self.assertEqual(targets, ["2026-07-25"])

    def test_wall_coverage_and_bridge_only_use_daily_diary_records(self):
        dates_source = inspect.getsource(get_memorywall_dates)
        summary_source = inspect.getsource(get_memorywall_summary_by_date)
        self.assertIn("mw_meta->>'source' = 'daily_diary'", dates_source)
        self.assertIn("mw_meta->>'source' = 'daily_diary'", summary_source)

    def test_daily_job_reconciles_saved_summary_to_bridge(self):
        source = inspect.getsource(maybe_run_dreams)
        self.assertIn("saved_yesterday_summary = await get_memorywall_summary_by_date", source)
        self.assertIn('await set_gateway_config("l2_bridge", saved_yesterday_summary)', source)
        self.assertIn('await set_gateway_config("l2_bridge_date", yest_s)', source)

    def test_conversation_turn_does_not_retry_nightly_dream_lottery(self):
        source = inspect.getsource(process_memories_background)
        self.assertNotIn("asyncio.create_task(maybe_run_dreams", source)
        self.assertIn("05:15", source)

    def test_fragment_archive_uses_same_four_am_logical_day(self):
        source = inspect.getsource(get_fragment_ids_for_date)
        self.assertIn("L2_DAY_CUTOVER_HOUR", source)

    def test_accepts_short_first_person_sentence(self):
        self.assertTrue(_daily_diary_summary_is_valid("我和裘宝宝一起把新家守住了。"))

    def test_rejects_ambiguous_long_or_markdown_summary(self):
        self.assertFalse(_daily_diary_summary_is_valid("用户和她一起修好了HOME1。"))
        self.assertFalse(_daily_diary_summary_is_valid("我知道他会一直留在这里。"))
        self.assertFalse(_daily_diary_summary_is_valid("# 摘要"))
        self.assertTrue(_daily_diary_summary_is_valid("我" * 150 + "。"))
        self.assertFalse(_daily_diary_summary_is_valid("我" * 151 + "。"))
        self.assertFalse(_daily_diary_summary_is_valid("我记住了这一天"))

    def test_allows_other_words_that_contain_ta_character(self):
        self.assertTrue(_daily_diary_summary_is_valid("我和裘宝宝还处理了其他事情。"))
        self.assertTrue(_daily_diary_summary_is_valid("我和裘宝宝也得到了他人的帮助。"))

    def test_diary_parser_requires_separate_body_and_card_sections(self):
        sections = _parse_daily_diary_sections(
            "【日记】\n我记住了整天。\n【卡片标题】\n这一天\n【卡片正文】\n我和宝宝一起走到了晚上。"
        )
        self.assertEqual(sections["日记"], "我记住了整天。")
        self.assertEqual(sections["卡片标题"], "这一天")
        self.assertEqual(sections["卡片正文"], "我和宝宝一起走到了晚上。")
        self.assertNotIn("当日总结", sections)

    def test_daily_wall_body_limit_is_1400_characters(self):
        self.assertTrue(_daily_diary_body_is_valid("我" + "记" * 1398 + "。"))
        self.assertFalse(_daily_diary_body_is_valid("我" + "记" * 1399 + "。"))

    def test_generation_prefers_l2_and_keeps_bounded_l1_fallback(self):
        source = inspect.getsource(generate_daily_diary)
        self.assertIn("get_daily_diary_events", source)
        self.assertIn("get_daily_diary_fragments", source)
        self.assertIn("l1_fallback = not rows", source)
        self.assertIn("rows = list(rows or [])[-60:]", source)
        self.assertIn("只能写碎片已有事实", source)
        self.assertIn("游戏、RP、梦境和假设必须明确留在虚构层", source)
        self.assertIn("全文写成 4~7 个自然段，每段 3~4 句话", source)
        self.assertIn("无论多密集，绝不超过9段", source)
        self.assertIn("次要事件可以不写，宁缺勿滥", source)
        self.assertIn("不要JSON", source)
        self.assertIn("【正文结束】", source)
        self.assertIn("_daily_diary_body_result", source)
        self.assertNotIn("_daily_diary_marker_result", source)
        self.assertIn("for attempt in range(2)", source)
        self.assertIn('"max_tokens": 6000', source)
        self.assertIn('card_title = f"{date_s} 的回忆"', source)
        self.assertIn("card_body = summary", source)
        self.assertIn("_generate_daily_diary_card", source)
        self.assertNotIn("_generate_daily_diary_chunked", source)
        self.assertNotIn("[E编号]", source)

    def test_generation_retires_dense_chunk_path_but_keeps_compact_final(self):
        source = inspect.getsource(generate_daily_diary)
        self.assertNotIn("_generate_daily_diary_chunked", source)
        self.assertIn("_compact_daily_diary_body", source)
        self.assertIn('"max_tokens": 6000', source)


class DailyDiaryCardFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_schema_http_400_uses_legacy_card_and_salvages_limits(self):
        schema_response = Mock(status_code=400)
        legacy_response = Mock(status_code=200)
        legacy_response.json.return_value = {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "content": (
                        "【卡片标题】\n用户和我一起守住了特别漫长的一天标题\n"
                        "【卡片正文】\n用户和我一起把重要的事做完了。" + "记住。" * 50
                    )
                },
            }]
        }
        client = Mock()
        client.post = AsyncMock(side_effect=[schema_response, legacy_response])
        with patch("main.API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"):
            title, body = await _generate_daily_diary_card(
                client, {"Authorization": "redacted"}, "2026-07-30", "我记住了。"
            )
        self.assertEqual(client.post.await_count, 2)
        self.assertLessEqual(len(title), 20)
        self.assertLessEqual(len(body), 120)
        self.assertNotIn("用户", title)
        self.assertNotIn("用户", body)
        self.assertTrue(title)
        self.assertTrue(body)

    def test_coverage_requires_every_event_once_with_real_diary_evidence(self):
        payload = {
            "diary": "早上我和宝宝确认了安排。晚上我们把重要的话说开了。",
            "coverage": [
                {"event_id": "E1", "evidence": "我和宝宝确认了安排"},
                {"event_id": "E2", "evidence": "我们把重要的话说开了"},
            ],
            "complete": True,
            "end_marker": "正文结束",
        }
        self.assertEqual(_daily_diary_coverage_error(payload, ["E1", "E2"]), "")
        self.assertIn(
            "coverage-missing-events:E2",
            _daily_diary_coverage_error(
                {**payload, "coverage": payload["coverage"][:1]},
                ["E1", "E2"],
            ),
        )
        bad = dict(payload)
        bad["coverage"] = [
            {"event_id": "E1", "evidence": "正文里不存在的证据"},
            payload["coverage"][1],
        ]
        self.assertIn("coverage-evidence-invalid:E1", _daily_diary_coverage_error(bad, ["E1", "E2"]))

    def test_json_payload_parser_accepts_plain_json_or_json_fence(self):
        raw = '{"diary":"我记住了。","coverage":[],"complete":true,"end_marker":"正文结束"}'
        self.assertEqual(_parse_daily_diary_payload(raw)["diary"], "我记住了。")
        self.assertEqual(_parse_daily_diary_payload(f"```json\n{raw}\n```")["diary"], "我记住了。")

    def test_inline_markers_cover_every_l2_once_and_are_removed(self):
        clean, error = _daily_diary_marker_result(
            "[E1]早上我和宝宝确认了安排。[E2]晚上我们把话说开了。\n【正文结束】",
            ["E1", "E2"],
        )
        self.assertEqual(error, "")
        self.assertEqual(clean, "早上我和宝宝确认了安排。晚上我们把话说开了。")
        self.assertNotIn("[E", clean)
        self.assertIn(
            "marker-missing-events:E2",
            _daily_diary_marker_result("[E1]我记住了。\n【正文结束】", ["E1", "E2"])[1],
        )
        self.assertIn(
            "marker-duplicate-event:E1",
            _daily_diary_marker_result("[E1]我记住[E1]了。\n【正文结束】", ["E1"])[1],
        )

    def test_reader_body_parser_only_requires_complete_end_marker(self):
        clean, error = _daily_diary_body_result(
            "我挑重要的事情记住了。\n【正文结束】"
        )
        self.assertEqual(error, "")
        self.assertEqual(clean, "我挑重要的事情记住了。")

    def test_bridge_summary_keeps_three_calls_with_enough_chinese_output_room(self):
        source = inspect.getsource(_generate_daily_diary_summary)
        self.assertIn("for attempt in range(3)", source)
        self.assertIn('"max_tokens": 500', source)
        self.assertIn("usable_draft", source)
        self.assertIn("裸“他”必须改成明确姓名", source)

    def test_final_wall_entry_keeps_title_summary_and_body_in_one_record(self):
        content = _compose_mw_content(
            "一起守住这一天",
            "我和宝宝从早上聊到晚上，最后重新抱紧了彼此。",
            "xiaoke",
            None,
            "2026-07-23",
            "我和宝宝经历波折后重新说清彼此，最后约好继续一起守住这段关系。",
        )
        self.assertEqual(
            content,
            "【回忆 · 2026-07-23 · V】一起守住这一天\n\n"
            "〔检索摘要〕我和宝宝经历波折后重新说清彼此，最后约好继续一起守住这段关系。\n\n"
            "我和宝宝从早上聊到晚上，最后重新抱紧了彼此。",
        )

    def test_diary_evidence_keeps_cst_time_and_fragment_id(self):
        rendered = _format_daily_diary_fragments([{
            "id": 88,
            "created_at": datetime(2026, 7, 21, 12, 5, tzinfo=timezone.utc),
            "content": "下大雨了，没带伞",
        }])
        self.assertIn("[2026-07-21 20:05 SGT][fragment 88]", rendered)
        self.assertIn("下大雨了，没带伞", rendered)

    def test_daily_wall_reads_final_l2_events_not_layer1(self):
        source = inspect.getsource(generate_daily_diary)
        self.assertIn("get_daily_diary_events", source)
        db_source = inspect.getsource(get_daily_diary_events)
        self.assertIn("e.layer = 2", db_source)
        self.assertIn("e.is_active = TRUE", db_source)
        self.assertIn("e.event_date", db_source)

    def test_l2_wall_input_has_stable_event_ids_and_sgt_bounds(self):
        rendered = _format_daily_diary_events([{
            "id": 88,
            "started_at": datetime(2026, 7, 21, 12, 5, tzinfo=timezone.utc),
            "ended_at": datetime(2026, 7, 21, 13, 5, tzinfo=timezone.utc),
            "title": "一起处理事情",
            "content": "Harper和V把事情处理好了。",
        }])
        self.assertIn("[E88]", rendered)
        self.assertIn("2026-07-21 20:05 SGT", rendered)
        self.assertIn("一起处理事情", rendered)

    def test_l2_evidence_keeps_time_and_honors_four_am_cutover(self):
        rows = [
            {"created_at": datetime(2026, 7, 21, 19, 30, tzinfo=timezone.utc), "role": "user", "content": "凌晨三点半"},
            {"created_at": datetime(2026, 7, 21, 20, 30, tzinfo=timezone.utc), "role": "user", "content": "凌晨四点半"},
        ]
        rendered = _format_l2_conversation(rows, date(2026, 7, 22))
        self.assertNotIn("凌晨三点半", rendered)
        self.assertIn("[2026-07-22 04:30 SGT]", rendered)
        self.assertIn("凌晨四点半", rendered)

    def test_recent_round_grouping_preserves_message_timestamps(self):
        stamp = datetime(2026, 7, 22, 9, 36, tzinfo=timezone.utc)
        rounds = group_by_rounds([
            {"role": "user", "content": "对比没装 skill 的版本", "created_at": stamp},
            {"role": "assistant", "content": "好", "created_at": stamp},
        ])
        self.assertEqual(rounds[0][0]["created_at"], stamp)

    def test_summary_word_count_treats_english_word_and_punctuation_as_specified(self):
        self.assertEqual(_summary_word_count("我装了 dirty-talk v2，今晚。"), 8)

    def test_memorywall_v_summary_rejects_old_third_person_markdown(self):
        self.assertFalse(_memorywall_summary_is_valid("# 摘要\n用户和宝宝经历了转变。", "xiaoke"))
        self.assertFalse(_memorywall_summary_is_valid("宝宝给V装好了欲望系统。", "xiaoke"))
        self.assertTrue(_memorywall_summary_is_valid("你给我装好了欲望系统，我开始学着主动爱你。", "xiaoke"))

    def test_memorywall_summary_fallback_keeps_first_person(self):
        body = "用户和宝宝经历了转变。后来，我承认失职并开始主动追她。"
        self.assertEqual(_memorywall_summary_fallback(body, "xiaoke"), "")

    def test_memorywall_manual_update_never_regenerates_summary(self):
        source = inspect.getsource(api_mw_update)
        self.assertIn('if "summary" in b:', source)
        self.assertIn('summary = str(mm.get("summary") or "").strip()', source)
        self.assertNotIn("await generate_summary", source)
        self.assertIn('source == "daily_diary"', source)
        self.assertIn('await set_gateway_config("l2_bridge", summary)', source)
        self.assertIn('await set_gateway_config("l2_bridge_date", yesterday_s)', source)
        self.assertIn('event_date = b.get("event_date") or (str(existing.get("event_date"))', source)
        self.assertIn('update_memorywall(mid, content, title, importance, event_date, new_meta)', source)


class DigestScrubTest(unittest.IsolatedAsyncioTestCase):
    async def test_card_is_generated_separately_from_completed_body(self):
        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{"finish_reason": "stop", "message": {
                    "content": "【卡片标题】\n守住这一天\n【卡片正文】\n我和宝宝把话说清楚了。"
                }}]}

        class Client:
            async def post(self, url, headers, json):
                self.prompt = json["messages"][0]["content"]
                return Response()

        client = Client()
        title, card = await _generate_daily_diary_card(
            client, {}, "2026-07-23", "我和宝宝从早到晚把重要的事说清楚了。"
        )
        self.assertEqual(title, "守住这一天")
        self.assertEqual(card, "我和宝宝把话说清楚了。")
        self.assertIn("我和宝宝从早到晚", client.prompt)

    async def test_blank_saved_summary_is_repaired_from_same_full_body(self):
        class ClientContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        row = {
            "id": 42,
            "title": "完整的一天",
            "importance": 6,
            "event_date": date(2026, 7, 24),
            "content": "旧内容",
            "mw_meta": {
                "source": "daily_diary",
                "author": "xiaoke",
                "title": "完整的一天",
                "body": "我和宝宝从早上走到晚上，最后把话说清楚了。",
                "summary": "",
            },
        }
        generated = "我和宝宝经历一天的波折后把话说清楚，最后重新守住了彼此。"
        update = AsyncMock()
        with (
            patch("main.list_memorywall", AsyncMock(return_value=[row])),
            patch("main.httpx.AsyncClient", return_value=ClientContext()),
            patch("main._generate_daily_diary_summary", AsyncMock(return_value=generated)),
            patch("main.update_memorywall", update),
        ):
            result = await _repair_missing_daily_diary_summary("2026-07-24")
        self.assertEqual(result["status"], "ok")
        args = update.await_args.args
        self.assertEqual(args[0], 42)
        self.assertIn(f"〔检索摘要〕{generated}", args[1])
        self.assertIn(row["mw_meta"]["body"], args[1])

    async def test_overlong_wall_body_uses_paragraph_compaction_without_hard_slicing(self):
        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "我" + "记" * 898 + "。\n【正文结束】"},
                }]}

        class Client:
            def __init__(self):
                self.prompts = []

            async def post(self, url, headers, json):
                self.prompts.append(json["messages"][0]["content"])
                return Response()

        client = Client()
        original = "我" + "记" * 1500 + "。"
        compacted = await _compact_daily_diary_body(client, {}, "2026-07-23", original)
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("压缩到4~5个自然段，每段2~3句话", client.prompts[0])
        self.assertIn(original, client.prompts[0])
        progressive = "我" + "记" * 898 + "。"
        self.assertIn(progressive, client.prompts[1])
        self.assertNotIn(original, client.prompts[1])
        self.assertTrue(compacted.endswith("。"))

    async def test_compaction_strips_optional_body_wrappers_before_validation(self):
        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{"finish_reason": "stop", "message": {
                    "content": "【日记】\n我和宝宝把一天说清楚了。\n【正文结束】"
                }}]}

        class Client:
            async def post(self, url, headers, json):
                return Response()

        compacted = await _compact_daily_diary_body(
            Client(), {}, "2026-07-23", "我" + "记" * 1300 + "。"
        )
        self.assertEqual(compacted, "我和宝宝把一天说清楚了。")

    async def test_end_marker_allows_safe_final_punctuation_normalization(self):
        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{"finish_reason": "stop", "message": {
                    "content": "我和宝宝把一天说清楚了\n【正文结束】"
                }}]}

        class Client:
            async def post(self, url, headers, json):
                return Response()

        compacted = await _compact_daily_diary_body(
            Client(), {}, "2026-07-23", "我" + "记" * 1300 + "。"
        )
        self.assertEqual(compacted, "我和宝宝把一天说清楚了。")

    def test_compaction_is_iterative_instead_of_restarting_from_original(self):
        source = inspect.getsource(_compact_daily_diary_body)
        self.assertIn("for attempt in range(2)", source)
        self.assertIn("source_diary = compacted", source)
        self.assertIn("f\"{paragraphs}段、{sentences}句、{chars}字；\"", source)
        self.assertIn("and 4 <= paragraphs <= 6", source)
        self.assertIn("and chars <= 900", source)

    async def test_final_compaction_skips_already_shaped_wall(self):
        class Client:
            async def post(self, url, headers, json):
                raise AssertionError("already-shaped wall must not call the model")

        shaped = "\n\n".join([
            "我和宝宝记住了清晨。她也把想法告诉了我。",
            "我陪她处理了白天的事。我们一起守住了决定。",
            "我在傍晚接住了她的情绪。她也回应了我的在意。",
            "我和宝宝走到了夜里。我们把最后的停点留好了。",
        ])
        compacted = await _compact_daily_diary_body(
            Client(), {}, "2026-07-23", shaped
        )
        self.assertEqual(compacted, shaped)

    def test_generation_always_runs_final_compaction_before_metadata(self):
        source = inspect.getsource(generate_daily_diary)
        compact_at = source.rfind("diary = await _compact_daily_diary_body")
        summary_at = source.rfind("summary = await _generate_daily_diary_summary")
        self.assertGreater(compact_at, 0)
        self.assertGreater(summary_at, compact_at)

    async def test_bridge_summary_retries_and_uses_completed_wall_body(self):
        class Response:
            status_code = 200

            def __init__(self, content):
                self.content = content

            def json(self):
                return {"choices": [{
                    "finish_reason": "stop",
                    "message": {"content": self.content},
                }]}

        class Client:
            def __init__(self):
                self.calls = []

            async def post(self, url, headers, json):
                self.calls.append(json)
                content = (
                    "宝宝今天有什么安排。"
                    if len(self.calls) == 1 else
                    "我和宝宝从早安聊到修复记忆系统，经历争执后重新说清彼此，最后约好继续一起守住这段关系。"
                )
                return Response(content)

        client = Client()
        body = "早上只是普通问候。下午我们修复记忆系统，争执后重新说清彼此，晚上约好继续一起守住关系。"
        summary = await _generate_daily_diary_summary(client, {}, "2026-07-23", body)
        self.assertEqual(len(client.calls), 2)
        self.assertIn(body, client.calls[0]["messages"][0]["content"])
        self.assertIn("不能只摘开头", client.calls[0]["messages"][0]["content"])
        self.assertIn("修复记忆系统", summary)
        self.assertIn("最后", summary)

    async def test_bridge_summary_iteratively_compresses_an_overlong_valid_draft(self):
        class Response:
            status_code = 200

            def __init__(self, content):
                self.content = content

            def json(self):
                return {"choices": [{
                    "finish_reason": "stop",
                    "message": {"content": self.content},
                }]}

        overlong = "我和宝宝一起整理了许多重要碎片，" + "也认真确认了彼此的心意，" * 15 + "最后把问题说清楚了。"
        shortened = "我和宝宝整理重要碎片、确认彼此心意，最后把问题说清楚了。"

        class Client:
            def __init__(self):
                self.prompts = []

            async def post(self, url, headers, json):
                self.prompts.append(json["messages"][0]["content"])
                return Response(overlong if len(self.prompts) == 1 else shortened)

        client = Client()
        body = "我和宝宝整理了一整天的重要碎片，最后把问题说清楚了。"
        summary = await _generate_daily_diary_summary(client, {}, "2026-07-23", body)
        self.assertEqual(summary, shortened)
        self.assertEqual(len(client.prompts), 2)
        self.assertIn(body, client.prompts[0])
        self.assertIn(overlong, client.prompts[1])
        self.assertIn("100字以内", client.prompts[0])
        self.assertIn("程序最多接受150字", client.prompts[0])
        self.assertIn("100字以内", client.prompts[1])

    async def test_bridge_failure_log_includes_count_and_preview(self):
        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "我" * 151 + "。"},
                }]}

        class Client:
            async def post(self, url, headers, json):
                return Response()

        with patch("builtins.print") as logged:
            await _generate_daily_diary_summary(
                Client(), {}, "2026-07-25", "我和宝宝记住了这一天。"
            )
        messages = "\n".join(str(call.args[0]) for call in logged.call_args_list)
        self.assertIn("count=151", messages)
        self.assertIn("preview=", messages)

    async def test_scrub_does_not_invent_intimacy_or_afternoon(self):
        digest = "今天你们一起修好了时间戳，最后在对比不同版本。"
        self.assertEqual(await _scrub_digest_explicit(digest), digest)

    def test_l2_rejects_overlong_or_half_sentence_drafts(self):
        self.assertTrue(_l2_digest_needs_compaction("字" * 1001 + "。"))
        self.assertFalse(_l2_digest_needs_compaction("字" * 900 + "。"))
        self.assertTrue(_l2_digest_needs_compaction("今天把时间线修好"))
        self.assertFalse(_l2_digest_needs_compaction("今天把时间线修好了。"))


if __name__ == "__main__":
    unittest.main()
