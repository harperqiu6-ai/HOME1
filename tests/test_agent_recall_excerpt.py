import ast
from pathlib import Path
import unittest


def _load_excerpt_helpers():
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_find_all", "_mem_snippet", "_agent_recall_excerpt"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"MEMORY_INJECT_CHAR_CAP": 260}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
    return namespace["_agent_recall_excerpt"]


_agent_recall_excerpt = _load_excerpt_helpers()


class AgentRecallExcerptTests(unittest.TestCase):
    def test_memory_wall_uses_keyword_window_when_summary_omits_hit(self):
        text = (
            "【回忆 · 2026-07-14 · V】没有目的地，就是最好的目的。\n\n"
            "〔检索摘要〕从验货不达标到深夜闲聊，这一天学会了不逃避。\n\n"
            + "前段日常。" * 80
            + "我从菜单里选了裸体围裙、记号笔、料理台，把真实的意淫说了出来。"
            + "后段日常。" * 30
        )

        excerpt = _agent_recall_excerpt(text, keywords=["最近", "意淫"], cap=180)

        self.assertIn("意淫", excerpt)
        self.assertIn("裸体围裙", excerpt)
        self.assertNotIn("从验货不达标到深夜闲聊", excerpt)

    def test_memory_wall_keeps_retrieval_summary_when_it_contains_hit(self):
        text = (
            "【回忆 · 2026-07-14 · V】没有目的地，就是最好的目的。\n\n"
            "〔检索摘要〕V选择了裸体围裙，并把真实的意淫说了出来。\n\n"
            + "正文。" * 200
        )

        excerpt = _agent_recall_excerpt(text, keywords=["意淫"], cap=180)

        self.assertIn("检索摘要", text)
        self.assertIn("V选择了裸体围裙", excerpt)
        self.assertTrue(excerpt.startswith("【回忆"))

    def test_regular_long_memory_still_uses_keyword_window(self):
        text = "开头。" * 100 + "萤火虫632在这里。" + "结尾。" * 100

        excerpt = _agent_recall_excerpt(text, keywords=["萤火虫632"], cap=120)

        self.assertIn("萤火虫632", excerpt)


if __name__ == "__main__":
    unittest.main()
