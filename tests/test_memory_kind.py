import inspect
import unittest

import database
from consolidation_transaction import commit_l2_and_absorb_sources
from main import api_create_memory


class MemoryKindTest(unittest.TestCase):
    def test_musing_content_is_canonicalized_but_kind_is_independent(self):
        self.assertEqual(
            database.normalize_suixiang_content("  [v 随想]：我记得。"),
            "【V的随想】我记得。",
        )
        self.assertEqual(
            database.normalize_suixiang_content("我自己的感想。", force=True),
            "【V的随想】我自己的感想。",
        )

    def test_schema_backfills_prefix_and_constrains_kind(self):
        source = inspect.getsource(database.init_tables)
        self.assertIn("ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'fact'", source)
        self.assertIn("SET kind = 'musing', is_active = TRUE, decayed_at = NULL", source)
        self.assertIn("CHECK (kind IN ('fact', 'musing'))", source)

    def test_save_memory_persists_explicit_kind(self):
        source = inspect.getsource(database.save_memory)
        self.assertIn('normalized_kind not in {"fact", "musing"}', source)
        self.assertIn("event_date, kind", source)
        self.assertIn("normalized_kind", source)
        api_source = inspect.getsource(api_create_memory)
        self.assertIn('kind not in {"fact", "musing"}', api_source)
        self.assertIn("kind=kind", api_source)

    def test_all_automatic_archive_paths_protect_musings_by_kind(self):
        functions = [
            database.get_decay_candidates,
            database.archive_decayed_memories,
            database.get_fragment_ids_for_date,
            database.supersede_fragment,
            database.get_fragments_by_date,
            database.get_fragments_by_date_range,
            database.get_fragments_by_time_window,
            database.get_uncovered_fragments_by_time_window,
            database.deactivate_memories,
            database.absorb_consolidated_memories,
            database.cleanup_old_fragments,
            commit_l2_and_absorb_sources,
        ]
        for function in functions:
            with self.subTest(function=function.__name__):
                self.assertIn("kind <> 'musing'", inspect.getsource(function))

    def test_dashboard_musing_filter_and_stats_use_kind(self):
        page_source = inspect.getsource(database.get_memories_detail_page)
        stats_source = inspect.getsource(database.get_layer_statistics)
        self.assertIn("kind = 'musing'", page_source)
        self.assertIn("kind <> 'musing'", page_source)
        self.assertIn("kind = 'musing'", stats_source)


if __name__ == "__main__":
    unittest.main()
