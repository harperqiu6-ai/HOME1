import ast
from pathlib import Path
import unittest


def _load_l2_helper():
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_l2_digest_session_id"
    ]
    namespace = {"CYBERBOSS_LINE_ID": "cyberboss"}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
    return namespace["_l2_digest_session_id"]


_l2_digest_session_id = _load_l2_helper()


class L2CyberbossTests(unittest.TestCase):
    def test_today_digest_uses_companion_line(self):
        self.assertEqual(_l2_digest_session_id(), "cyberboss")


if __name__ == "__main__":
    unittest.main()
