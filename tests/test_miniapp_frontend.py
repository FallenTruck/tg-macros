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
        styles = (Path(__file__).parents[1] / "miniapp" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('/api/workout/sessions', source)
        self.assertIn('/api/workout/sessions/active', source)
        self.assertIn('"start-workout"', source)
        self.assertIn('"resume-workout"', source)
        self.assertIn('data-action="view-programme"', source)
        self.assertIn('workoutMode: WORKOUT_PROGRAMME_MODE', source)
        self.assertIn('setWorkoutMode(WORKOUT_ACTIVE_MODE, {scrollToSession: true})', source)
        self.assertIn('window.scrollTo({top:', source)
        self.assertIn('data-action="skip-${kind}"', source)
        self.assertIn('data-set-form', source)
        self.assertIn('execution_expected_revision', source)
        self.assertIn('data-action="skip-${kind}"', source)
        self.assertIn('Save Set ${ordinal}', source)
        self.assertIn('Skip Set', source)
        self.assertIn('data-action="submit-workout"', source)
        self.assertIn('workoutCompletionDockEl?.addEventListener("click", handleWorkoutClick)', source)
        self.assertIn('Submit Workout', source)
        self.assertIn('/complete', source)
        self.assertIn('resolvedWorkingSetCount(execution)', source)
        self.assertIn('set.set_type || "working"', source)
        self.assertIn('Enter a whole-number rep count for both left and right sides.', source)
        self.assertIn('Repeat previous set', source)
        self.assertIn('data-action="repeat-previous-set"', source)
        self.assertIn('Skip Exercise', source)
        self.assertIn('.workout-set-actions > .workout-skip-controls', styles)
        self.assertIn('display: flex', styles)
        self.assertIn('flex-wrap: wrap', styles)
        self.assertIn('flex: 1 1 140px', styles)
        self.assertIn('width: auto', styles)
        self.assertIn('overflow-x: hidden', styles)

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

    def test_workout_mode_visibility_and_sticky_completion_dock_are_explicit(self):
        root = Path(__file__).parents[1]
        source = (root / "miniapp" / "app.js").read_text(encoding="utf-8")
        markup = (root / "miniapp" / "index.html").read_text(encoding="utf-8")
        styles = (root / "miniapp" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('workoutProgrammeEl.hidden = state.workoutMode !== WORKOUT_PROGRAMME_MODE', source)
        self.assertIn('workoutSessionEl.hidden = !active?.session || state.workoutMode !== WORKOUT_ACTIVE_MODE', source)
        self.assertIn('id="workout-completion-dock"', markup)
        self.assertIn('id="workout-session" class="workout-session" aria-live="polite" hidden', markup)
        self.assertIn("function workoutCompletionSummary(active)", source)
        self.assertIn("prescribed_set_count_min", source)
        self.assertIn('summary.completed} / ${summary.total} exercises completed', source)
        self.assertIn('data-action="submit-workout"', source)
        self.assertIn("[hidden]", styles)
        self.assertIn(".workout-session[hidden]", styles)
        self.assertIn(".workout-programme[hidden]", styles)
        self.assertIn(".workout-completion-dock", styles)
        self.assertIn("position: fixed", styles)
        self.assertIn("padding-bottom: calc(210px", styles)

    def test_miniapp_assets_are_versioned_during_deployment(self):
        root = Path(__file__).parents[1]
        markup = (root / "miniapp" / "index.html").read_text(encoding="utf-8")
        deploy_script = (root / "scripts" / "deploy_miniapp.sh").read_text(encoding="utf-8")
        self.assertIn("/styles.css?v=__MINIAPP_VERSION__", markup)
        self.assertIn("/app.js?v=__MINIAPP_VERSION__", markup)
        self.assertIn("shasum -a 256 miniapp/index.html miniapp/app.js miniapp/styles.css", deploy_script)
        self.assertIn('no-cache, no-store, must-revalidate', deploy_script)
        self.assertIn('public, max-age=31536000, immutable', deploy_script)

    def test_auth_startup_has_distinct_browser_and_telegram_states(self):
        root = Path(__file__).parents[1]
        source = (root / "miniapp" / "app.js").read_text(encoding="utf-8")
        markup = (root / "miniapp" / "index.html").read_text(encoding="utf-8")
        template = (root / "template.yaml").read_text(encoding="utf-8")
        self.assertIn('const initData = String(tg?.initData ?? "").trim();', source)
        self.assertIn('apiFetch("/api/auth/session")', source)
        self.assertIn('revealApp("browser", response.viewer)', source)
        self.assertIn('revealApp("telegram", response.viewer)', source)
        self.assertIn("showTelegramAuthError();", source)
        self.assertIn('credentials: "same-origin"', source)
        self.assertIn('id="browser-login-form"', markup)
        self.assertIn('autocomplete="username"', markup)
        self.assertIn('autocomplete="current-password"', markup)
        self.assertIn('id="app-shell" hidden', markup)
        self.assertIn('id="logout-button"', markup)
        self.assertIn('CookieBehavior: whitelist', template)
        self.assertIn('            - jf_session', template)
        self.assertIn('            - Origin', template)
        self.assertIn('            - Referer', template)


if __name__ == "__main__":
    unittest.main()
