import json
import tempfile
import unittest
from pathlib import Path

from desire_lexicon import LexiconError, mutate, snapshot


class DesireLexiconTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lexicon = self.root / "lexicon.json"
        self.audit = self.root / "audit.jsonl"
        self.lexicon.write_text(json.dumps({
            "window_minutes": 45,
            "openers": ["贴贴"],
            "implicit_terms": ["蹭蹭"],
            "nonsexual_phrases": ["欲望系统"],
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_harper_and_v_can_add_and_remove_one_term_with_audit(self):
        added = mutate("add", "implicit_terms", "舔舔", "harper", "页面添加", self.lexicon, self.audit)
        self.assertTrue(added["changed"])
        self.assertIn("舔舔", added["groups"]["implicit_terms"])

        removed = mutate("remove", "implicit_terms", "蹭蹭", "v", "Harper让我删", self.lexicon, self.audit)
        self.assertTrue(removed["changed"])
        self.assertNotIn("蹭蹭", removed["groups"]["implicit_terms"])

        state = snapshot(10, self.lexicon, self.audit)
        self.assertEqual([item["actor"] for item in state["audit"]], ["harper", "v"])
        self.assertEqual([item["action"] for item in state["audit"]], ["add", "remove"])
        self.assertEqual(self.lexicon.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.audit.stat().st_mode & 0o777, 0o600)

    def test_duplicate_and_missing_remove_are_noops_but_audited(self):
        self.assertFalse(mutate("add", "implicit_terms", "蹭蹭", "v", "", self.lexicon, self.audit)["changed"])
        self.assertFalse(mutate("remove", "openers", "不存在", "harper", "", self.lexicon, self.audit)["changed"])
        self.assertEqual(len(snapshot(10, self.lexicon, self.audit)["audit"]), 2)

    def test_only_three_term_groups_are_mutable(self):
        for group in ("window_minutes", "unknown", ""):
            with self.subTest(group=group), self.assertRaises(LexiconError):
                mutate("add", group, "词", "v", "", self.lexicon, self.audit)

    def test_rejects_multiline_and_unknown_actor(self):
        with self.assertRaises(LexiconError):
            mutate("add", "openers", "两行\n词", "harper", "", self.lexicon, self.audit)
        with self.assertRaises(LexiconError):
            mutate("add", "openers", "词", "codex", "", self.lexicon, self.audit)


if __name__ == "__main__":
    unittest.main()
