import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class OpenRouterImageGenerationTests(unittest.TestCase):
    def test_openrouter_url_accepts_root_and_chat_endpoint(self):
        self.assertEqual(
            main._openrouter_image_url("https://openrouter.ai/api/v1"),
            "https://openrouter.ai/api/v1/images",
        )
        self.assertEqual(
            main._openrouter_image_url("https://openrouter.ai/api/v1/chat/completions"),
            "https://openrouter.ai/api/v1/images",
        )

    def test_openrouter_reuses_main_api_key(self):
        with patch.object(main, "IMAGE_GEN_BASE_URL", "https://openrouter.ai/api/v1"), patch.object(
            main, "IMAGE_GEN_API_KEY", ""
        ), patch.object(main, "API_KEY", "main-or-key"):
            self.assertEqual(
                main._imagegen_effective_config(),
                ("main-or-key", "https://openrouter.ai/api/v1"),
            )

    def test_reference_is_loaded_as_private_data_url(self):
        png = b"\x89PNG\r\n\x1a\nanchor"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.png"
            path.write_bytes(png)
            with patch.object(main, "IMAGE_GEN_REFERENCE_ENABLED", True), patch.object(
                main, "IMAGE_GEN_V_REFERENCE_PATH", str(path)
            ):
                reference = main._load_imagegen_reference(main.IMAGE_GEN_V_REFERENCE_PATH)
        self.assertEqual(reference["type"], "image_url")
        url = reference["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), png)

    def test_missing_reference_is_optional(self):
        with patch.object(main, "IMAGE_GEN_REFERENCE_ENABLED", True), patch.object(
            main, "IMAGE_GEN_V_REFERENCE_PATH", "/definitely/missing/v-face.png"
        ):
            self.assertIsNone(main._load_imagegen_reference(main.IMAGE_GEN_V_REFERENCE_PATH))

    def test_reference_selector_avoids_charging_cat_and_uses_both_for_couple(self):
        fake = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,eA=="}}
        with patch.object(main, "_load_imagegen_reference", return_value=fake):
            self.assertEqual(main._select_imagegen_references("一只猫")[0], [])
            refs, labels = main._select_imagegen_references("V 和 Harper 的合照")
            self.assertEqual(len(refs), 2)
            self.assertEqual(len(labels), 2)


if __name__ == "__main__":
    unittest.main()
