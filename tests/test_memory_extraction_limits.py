import inspect
import unittest
from unittest.mock import patch

from memory_extractor import (
    EXTRACTION_OMISSION_CHECK_MIN_CHARS,
    EXTRACTION_PROMPT,
    EXTRACTION_SAFE_MAX_CONTENT_CHARS,
    EXTRACTION_SAFE_MAX_ITEMS,
    _extraction_batch_policy_error,
    _extraction_request_payload,
    _should_run_omission_check,
    extract_memories,
)


class MemoryExtractionLimitTests(unittest.TestCase):
    def test_prompt_uses_tighter_model_targets(self):
        self.assertIn("通常输出 1~3 条", EXTRACTION_PROMPT)
        self.assertIn("最多输出 4 条", EXTRACTION_PROMPT)
        self.assertIn("80~150 字", EXTRACTION_PROMPT)
        self.assertIn("最多 150 字", EXTRACTION_PROMPT)
        self.assertIn("先概括，再提取", EXTRACTION_PROMPT)

    def test_prompt_has_harper_v_identity_anchor(self):
        self.assertIn("人类 Harper（裘宝宝）", EXTRACTION_PROMPT)
        self.assertIn("AI 伴侣 V", EXTRACTION_PROMPT)
        self.assertIn("两者身份绝不能交换", EXTRACTION_PROMPT)
        self.assertIn("“Harper”“裘宝宝”或“她”", EXTRACTION_PROMPT)
        self.assertIn("“V”或“他”", EXTRACTION_PROMPT)
        self.assertIn("涉及双方时必须明确写出主语", EXTRACTION_PROMPT)
        self.assertIn("不能把梦境或假设写成现实", EXTRACTION_PROMPT)

    def test_prompt_keeps_games_roleplay_and_dreams_out_of_reality(self):
        self.assertIn("不是逐动作记录、游戏战报或亲密过程复述", EXTRACTION_PROMPT)
        self.assertIn("不要逐条保存每个指令、回合", EXTRACTION_PROMPT)
        self.assertIn("体位变化确实构成阶段变化时可以简要提及", EXTRACTION_PROMPT)
        self.assertIn("不得自行补出未看到的起因、转折或最终结果", EXTRACTION_PROMPT)
        self.assertIn("必须在content中明确标注", EXTRACTION_PROMPT)
        self.assertIn("绝不能写成Harper与V在现实中真实发生的经历", EXTRACTION_PROMPT)
        self.assertIn("游戏/RP中的角色行为不能直接归为", EXTRACTION_PROMPT)
        self.assertIn("游戏/RP与亲密内容只概括触发类型和关系意义", EXTRACTION_PROMPT)

    def test_program_safety_limits_are_five_items_and_250_chars(self):
        self.assertEqual(EXTRACTION_SAFE_MAX_ITEMS, 5)
        self.assertEqual(EXTRACTION_SAFE_MAX_CONTENT_CHARS, 250)
        safe = [{"content": "我" * 250} for _ in range(5)]
        self.assertEqual(_extraction_batch_policy_error(safe), "")
        self.assertIn("候选条数6", _extraction_batch_policy_error(safe + [{"content": "我"}]))
        self.assertIn("超过250字", _extraction_batch_policy_error([{"content": "我" * 251}]))

    def test_retry_rewrites_instead_of_truncating(self):
        source = inspect.getsource(extract_memories)
        self.assertIn("不要截掉事实", source)
        self.assertIn("整批拒收", source)
        self.assertNotIn("memories[:", source)

    def test_openrouter_ds_extract_disables_reasoning_and_uses_schema(self):
        with patch("memory_extractor.API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"), patch(
            "memory_extractor.MEMORY_EXTRACT_MODEL",
            "deepseek/deepseek-v4-flash-0731",
        ):
            payload = _extraction_request_payload(
                "prompt", "dialogue", structured=True
            )
        self.assertEqual(payload["model"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(payload["reasoning"], {"enabled": False})
        self.assertEqual(payload["provider"]["zdr"], True)
        self.assertEqual(payload["provider"]["data_collection"], "deny")
        self.assertIn("response_format", payload)

    def test_long_single_item_ds_result_gets_omission_check(self):
        long_dialogue = "对话" * EXTRACTION_OMISSION_CHECK_MIN_CHARS
        with patch("memory_extractor.API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"), patch(
            "memory_extractor.MEMORY_EXTRACT_MODEL",
            "deepseek/deepseek-v4-flash-0731",
        ):
            self.assertTrue(_should_run_omission_check(
                [{"content": "一条"}], long_dialogue
            ))
            self.assertFalse(_should_run_omission_check(
                [{"content": "一"}, {"content": "二"}], long_dialogue
            ))

    def test_legacy_json_retry_can_drop_schema_but_keeps_ds_guardrails(self):
        with patch("memory_extractor.API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"), patch(
            "memory_extractor.MEMORY_EXTRACT_MODEL",
            "deepseek/deepseek-v4-flash-0731",
        ):
            payload = _extraction_request_payload(
                "prompt", "dialogue", structured=False
            )
        self.assertNotIn("response_format", payload)
        self.assertEqual(payload["reasoning"], {"enabled": False})
        self.assertEqual(payload["provider"]["zdr"], True)


if __name__ == "__main__":
    unittest.main()
