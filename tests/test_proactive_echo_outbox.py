import ast
from datetime import datetime, timezone
from pathlib import Path
import unittest

from database import _proactive_outbox_row


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

    def test_jsonb_intent_is_returned_as_an_object(self):
        now = datetime(2026, 8, 15, tzinfo=timezone.utc)
        row = {
            "id": 569,
            "message": "wake",
            "urgent": False,
            "created_at": now,
            "claimed_at": now,
            "delivered_at": now,
            "attempts": 1,
            "origin": "silence_wake",
            "intent": '{"wake_id":"wake-569","drive_key":"attachment"}',
        }

        item = _proactive_outbox_row(row)

        self.assertEqual(item["intent"], {
            "wake_id": "wake-569",
            "drive_key": "attachment",
        })

    def test_invalid_jsonb_intent_is_not_forwarded(self):
        now = datetime(2026, 8, 15, tzinfo=timezone.utc)
        row = {
            "id": 570,
            "message": "wake",
            "urgent": False,
            "created_at": now,
            "claimed_at": now,
            "delivered_at": None,
            "attempts": 0,
            "origin": "silence_wake",
            "intent": "not-json",
        }

        self.assertIsNone(_proactive_outbox_row(row)["intent"])


if __name__ == "__main__":
    unittest.main()
