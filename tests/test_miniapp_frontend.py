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

    def test_workout_view_supports_start_resume_and_deterministic_set_logging(self):
        source = (Path(__file__).parents[1] / "miniapp" / "app.js").read_text(encoding="utf-8")
        self.assertIn('/api/workout/sessions', source)
        self.assertIn('/api/workout/sessions/active', source)
        self.assertIn('data-action="start-workout"', source)
        self.assertIn('data-action="skip-${kind}"', source)
        self.assertIn('data-set-form', source)
        self.assertIn('execution_expected_revision', source)
        self.assertIn('data-action="skip-${kind}"', source)

    def test_workout_skip_reason_selector_supports_readable_reasons_and_safe_default(self):
        source = (Path(__file__).parents[1] / "miniapp" / "app.js").read_text(encoding="utf-8")
        for value, label in (
            ("recently_trained", "Recently trained"),
            ("time_constraint", "Time constraint"),
            ("equipment_unavailable", "Equipment unavailable"),
            ("fatigue", "Fatigue"),
            ("discomfort", "Discomfort"),
            ("intentionally_skipped", "Just skip"),
            ("other", "Other"),
        ):
            self.assertIn(f'["{value}", "{label}"]', source)
        self.assertIn('reasonSelect?.value || "intentionally_skipped"', source)
        self.assertIn('"set",', source)
        self.assertIn("data-skip-reason-select", source)
        self.assertNotIn('skip_reason: "intentionally_skipped"', source)


if __name__ == "__main__":
    unittest.main()
