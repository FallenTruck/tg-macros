from pathlib import Path
import unittest


class MiniAppFrontendTests(unittest.TestCase):
    def test_target_summary_uses_effective_timestamp_not_profile_import_timestamp(self):
        source = (Path(__file__).parents[1] / "miniapp" / "app.js").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("state.profile.target_effective_at"), 2)
        self.assertIn("Effective from ${formatIso(state.profile.target_effective_at)}", source)
        self.assertNotIn("formatIso(state.profile.updated_at)", source)
