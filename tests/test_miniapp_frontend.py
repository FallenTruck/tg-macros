from pathlib import Path
import unittest


class MiniAppFrontendTests(unittest.TestCase):
    def test_target_summary_uses_effective_timestamp_not_profile_import_timestamp(self):
        source = (Path(__file__).parents[1] / "miniapp" / "app.js").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("state.profile.target_effective_at"), 2)
        self.assertIn("Effective from ${formatIso(state.profile.target_effective_at)}", source)
        self.assertNotIn("formatIso(state.profile.updated_at)", source)

    def test_read_only_workout_programme_view_is_authenticated_and_rendered(self):
        source = (Path(__file__).parents[1] / "miniapp" / "app.js").read_text(encoding="utf-8")
        markup = (Path(__file__).parents[1] / "miniapp" / "index.html").read_text(encoding="utf-8")
        self.assertIn('apiFetch("/api/workout/programme")', source)
        self.assertIn('X-Telegram-Init-Data', source)
        self.assertIn('id="workout-view"', markup)
        self.assertIn('data-route="workout"', markup)
        self.assertIn("planned_weekday", source)
        self.assertIn("option_targets", source)
        self.assertNotIn("startWorkout", source)
        self.assertNotIn("progression", source.lower())


if __name__ == "__main__":
    unittest.main()
