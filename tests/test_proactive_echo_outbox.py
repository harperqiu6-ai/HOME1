import ast
from pathlib import Path
import unittest


def _load_push_defaults():
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignment = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "PUSH_DEFAULTS" for target in node.targets)
    )
    return ast.literal_eval(assignment.value)


class ProactiveEchoOutboxTests(unittest.TestCase):
    def test_echo_delivery_is_opt_in_for_safe_rollout(self):
        defaults = _load_push_defaults()
        self.assertIn("push_echo_enabled", defaults)
        self.assertIs(defaults["push_echo_enabled"], False)


if __name__ == "__main__":
    unittest.main()
