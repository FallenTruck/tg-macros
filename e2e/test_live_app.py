"""Live Chromium smoke tests for the isolated JavaanFitness E2E account.

These tests are intentionally outside ``tests/``. They require AWS SSO access,
SSM credentials, the deployed dev stack, and a locally installed Playwright
Chromium browser.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any

from scripts.e2e_support import (
    aws_session,
    dev_resources,
    load_e2e_credentials,
    read_e2e_records,
    validate_e2e_credential,
    user_partition_items,
)


class LiveJavaanFitnessE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("RUN_JAVAAN_E2E") != "1":
            raise unittest.SkipTest("set RUN_JAVAAN_E2E=1 to run live E2E tests")
        from playwright.sync_api import sync_playwright

        cls._sync_playwright = staticmethod(sync_playwright)
        session = aws_session()
        _session, _table, outputs, _repository = dev_resources(session)
        read_e2e_records(_table)
        validate_e2e_credential(_repository.get_web_credential("javaan-e2e"))
        cls.table = _table
        cls.base_url = outputs["MiniAppUrl"].rstrip("/")
        cls.username, cls.password = load_e2e_credentials(session)

    @staticmethod
    def _assert_no_horizontal_overflow(page: Any) -> None:
        dimensions = page.evaluate(
            """() => ({
                innerWidth: window.innerWidth,
                documentWidth: document.documentElement.scrollWidth,
                bodyWidth: document.body ? document.body.scrollWidth : 0,
            })"""
        )
        if dimensions["documentWidth"] > dimensions["innerWidth"] or dimensions["bodyWidth"] > dimensions["innerWidth"]:
            raise AssertionError(f"page has horizontal overflow at {dimensions}")

    @staticmethod
    def _assert_one(locator: Any, label: str) -> None:
        count = locator.count()
        if count != 1:
            raise AssertionError(f"expected one {label}, found {count}")

    def _open_page(self, context: Any) -> Any:
        page = context.new_page()
        page.goto(f"{self.base_url}/", wait_until="domcontentloaded", timeout=45_000)
        page.get_by_test_id("browser-login-form").wait_for(state="visible", timeout=30_000)
        return page

    @staticmethod
    def _wait_for_app_ready(page: Any) -> None:
        page.get_by_test_id("bottom-navigation").wait_for(state="visible", timeout=30_000)
        page.locator("#app-shell").wait_for(state="visible", timeout=30_000)
        page.locator("#status-panel").wait_for(state="hidden", timeout=30_000)

    def _login(self, page: Any) -> None:
        username = page.get_by_test_id("browser-login-username")
        password = page.get_by_test_id("browser-login-password")
        submit = page.get_by_test_id("browser-login-submit")
        self._assert_one(username, "browser username field")
        self._assert_one(password, "browser password field")
        self._assert_one(submit, "browser login button")
        username.fill(self.username)
        password.fill(self.password)
        submit.click()
        self._wait_for_app_ready(page)
        self.assertFalse(page.get_by_test_id("browser-login-form").is_visible())
        self.assertTrue(page.get_by_test_id("logout").is_visible())

    def _reject_incorrect_password(self, page: Any) -> None:
        page.get_by_test_id("browser-login-username").fill(self.username)
        page.get_by_test_id("browser-login-password").fill("intentionally-wrong-password")
        page.get_by_test_id("browser-login-submit").click()
        error = page.get_by_test_id("browser-login-error")
        error.wait_for(state="visible", timeout=30_000)
        self.assertEqual(error.inner_text(), "Invalid username or password.")

    def _navigate(self, page: Any, route: str, view_id: str) -> None:
        nav = page.get_by_test_id(f"nav-{route}")
        self._assert_one(nav, f"{route} navigation link")
        nav.click()
        page.locator(view_id).wait_for(state="visible", timeout=30_000)
        if route == "workout":
            page.get_by_test_id("workout-day-start-PULL").wait_for(state="visible", timeout=30_000)

    def _check_nutrition_history(self, page: Any) -> None:
        self._navigate(page, "nutrition", "#nutrition-view")
        progress = page.get_by_test_id("nutrition-progress-calories")
        progress.wait_for(state="visible", timeout=30_000)
        self.assertIn("Consumed", progress.inner_text())
        self.assertIn("Target", progress.inner_text())
        self.assertIn("remaining", progress.inner_text())
        meal_list = page.get_by_test_id("nutrition-meal-list")
        meal_list.wait_for(state="visible", timeout=30_000)
        meals = meal_list.locator('[data-testid^="nutrition-meal-"]')
        if meals.count() < 1:
            raise AssertionError("nutrition history did not render the E2E baseline meal")
        self.assertIn("E2E baseline meal", meal_list.inner_text())
        self.assertTrue(page.locator("#nutrition-meals-empty").is_hidden())

        date_label = page.locator("#nutrition-date-label")
        current_date = date_label.inner_text()
        previous = page.get_by_test_id("nutrition-previous-day")
        previous.click()
        page.wait_for_function(
            "expected => document.getElementById('nutrition-date-label')?.textContent !== expected",
            arg=current_date,
            timeout=30_000,
        )
        self.assertTrue(page.get_by_test_id("nutrition-next-day").is_enabled())
        page.get_by_test_id("nutrition-next-day").click()
        page.wait_for_function(
            "expected => document.getElementById('nutrition-date-label')?.textContent === expected",
            arg=current_date,
            timeout=30_000,
        )

    def _start_workout(self, page: Any) -> None:
        start = page.get_by_test_id("workout-day-start-PULL")
        start.wait_for(state="visible", timeout=30_000)
        self._assert_one(start, "PULL workout start button")
        start.click()
        page.get_by_test_id("active-workout").wait_for(state="visible", timeout=30_000)

    @staticmethod
    def _set_form_values(form: Any) -> dict[str, str]:
        values: dict[str, str] = {}
        load = form.locator('input[name="load_value"]')
        reps = form.locator('input[name="reps"]')
        left = form.locator('input[name="left_reps"]')
        right = form.locator('input[name="right_reps"]')
        duration = form.locator('input[name="duration_seconds"]')
        if load.count() == 1:
            load.fill("20")
            values["load_value"] = "20"
        if reps.count() == 1:
            reps.fill("8")
            values["reps"] = "8"
        if left.count() == 1:
            left.fill("8")
            values["left_reps"] = "8"
        if right.count() == 1:
            right.fill("8")
            values["right_reps"] = "8"
        if duration.count() == 1:
            duration.fill("30")
            values["duration_seconds"] = "30"
        if not values:
            raise AssertionError("active workout exposed no supported set inputs")
        return values

    @staticmethod
    def _read_set_values(form: Any) -> dict[str, str]:
        values: dict[str, str] = {}
        for name in ("load_value", "reps", "left_reps", "right_reps", "duration_seconds"):
            input_locator = form.locator(f'input[name="{name}"]')
            if input_locator.count() == 1:
                values[name] = input_locator.input_value()
        return values

    def _save_and_repeat_a_set(self, page: Any) -> None:
        cards = page.locator('article[data-testid^="workout-execution-"]')
        card_count = cards.count()
        if card_count < 2:
            raise AssertionError(f"workout needs at least two exercises, found {card_count}")
        first_card = cards.nth(0)
        first_card_test_id = first_card.get_attribute("data-testid")
        if not first_card_test_id or not first_card_test_id.startswith("workout-execution-"):
            raise AssertionError("first workout execution is missing a stable selector")
        execution_id = first_card_test_id.removeprefix("workout-execution-")
        first_form = first_card.locator("form[data-set-form]")
        self._assert_one(first_form, "first workout set form")
        self._set_form_values(first_form)
        save = first_form.get_by_test_id("workout-save-set")
        self._assert_one(save, "first save-set button")
        save.click()

        repeat = first_card.get_by_test_id("workout-repeat-set")
        repeat.wait_for(state="visible", timeout=30_000)
        form_after_save = first_card.locator("form[data-set-form]")
        expected = self._read_set_values(form_after_save)
        repeat.click()
        repeated = self._read_set_values(form_after_save)
        self.assertEqual(repeated, expected)
        save_second = form_after_save.get_by_test_id("workout-save-set")
        self._assert_one(save_second, "second save-set button")
        save_second.click()

        next_form = page.get_by_test_id(f"workout-set-form-{execution_id}-3")
        next_form.wait_for(state="visible", timeout=30_000)
        skip_set = next_form.get_by_test_id("workout-skip-set")
        self._assert_one(skip_set, "skip-set button")
        skip_set.click()
        page.get_by_test_id(f"workout-set-row-{execution_id}-3").wait_for(state="visible", timeout=30_000)

    def _skip_all_exercises(self, page: Any) -> None:
        cards = page.locator('article[data-testid^="workout-execution-"]')
        card_count = cards.count()
        execution_test_ids = [cards.nth(index).get_attribute("data-testid") for index in range(card_count)]
        for execution_test_id in execution_test_ids:
            if not execution_test_id:
                raise AssertionError("workout execution is missing a stable selector")
            card = page.get_by_test_id(execution_test_id)
            skip_exercise = card.get_by_test_id("workout-skip-exercise")
            if skip_exercise.count() == 1 and skip_exercise.is_visible():
                skip_exercise.click()
                card.locator(".workout-skipped").wait_for(state="visible", timeout=30_000)

        dock = page.get_by_test_id("workout-completion-dock")
        dock.wait_for(state="visible", timeout=30_000)
        self.assertEqual(
            page.evaluate("getComputedStyle(document.getElementById('workout-completion-dock')).position"),
            "fixed",
        )
        submit = page.get_by_test_id("submit-workout")
        self._assert_one(submit, "submit-workout button")
        self.assertTrue(submit.is_enabled())

    def _complete_workout(self, page: Any) -> None:
        submit = page.get_by_test_id("submit-workout")
        submit.click()
        status = page.locator("#status-message")
        page.wait_for_function(
            "expected => document.getElementById('status-message')?.textContent.includes(expected)",
            arg="Workout submitted.",
            timeout=30_000,
        )
        self.assertIn("Workout submitted.", status.inner_text())
        page.locator("#workout-session").wait_for(state="hidden", timeout=30_000)
        page.locator("#workout-programme").wait_for(state="visible", timeout=30_000)

    def _run_workout_flow(self, page: Any) -> None:
        self._start_workout(page)
        self._save_and_repeat_a_set(page)
        self._skip_all_exercises(page)
        self._complete_workout(page)

    def _new_browser(self):
        headless = os.getenv("JAVAAN_E2E_HEADLESS", "1") != "0"
        playwright = self._sync_playwright().start()
        browser = playwright.chromium.launch(headless=headless)
        return playwright, browser

    def test_live_smoke(self):
        playwright, browser = self._new_browser()
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            page = self._open_page(context)
            self._reject_incorrect_password(page)
            self._login(page)
            page.reload(wait_until="domcontentloaded", timeout=45_000)
            self._wait_for_app_ready(page)
            self._assert_no_horizontal_overflow(page)
            self._check_nutrition_history(page)
            self._navigate(page, "profile", "#profile-view")
            self._navigate(page, "workout", "#workout-view")
            self._run_workout_flow(page)

            page.set_viewport_size({"width": 360, "height": 800})
            page.reload(wait_until="domcontentloaded", timeout=45_000)
            self._wait_for_app_ready(page)
            self._assert_no_horizontal_overflow(page)
            self._navigate(page, "home", "#home-view")
            self._check_nutrition_history(page)
            self._navigate(page, "profile", "#profile-view")
            self._navigate(page, "workout", "#workout-view")

            page.set_viewport_size({"width": 1280, "height": 900})
            page.reload(wait_until="domcontentloaded", timeout=45_000)
            self._wait_for_app_ready(page)
            self._assert_no_horizontal_overflow(page)
            self._navigate(page, "profile", "#profile-view")
            page.get_by_test_id("logout").click()
            page.get_by_test_id("browser-login-form").wait_for(state="visible", timeout=30_000)
            self.assertTrue(page.locator("#app-shell").is_hidden())
        finally:
            browser.close()
            playwright.stop()

    def test_live_nutrition_lab(self):
        """Real image + real OpenAI, with no numeric accuracy claims."""
        image = Path(os.getenv("JAVAAN_E2E_MEAL_IMAGE", "images/6143401176322477320.jpg")).resolve()
        if not image.is_file():
            self.fail("JAVAAN_E2E_MEAL_IMAGE must point to a real meal photograph")
        caption = os.getenv("JAVAAN_E2E_MEAL_CAPTION", "" if os.getenv("JAVAAN_E2E_MEAL_IMAGE") else
                            "Scrambled eggs, sausages, pasta, mashed potato, and grilled meat with sauce")
        output_dir = Path("artifacts/e2e")
        output_dir.mkdir(parents=True, exist_ok=True)
        playwright, browser = self._new_browser()
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            page = self._open_page(context)
            self._login(page)
            self._navigate(page, "nutrition", "#nutrition-view")
            page.get_by_test_id("nutrition-lab").wait_for(state="visible", timeout=30_000)
            self._assert_no_horizontal_overflow(page)

            def snapshot_domain():
                return {item["SK"]: item for item in user_partition_items(self.table, "e2e-javaan-e2e")
                        if not item["SK"].startswith("LAB_JOB#")}

            def result():
                import json
                return json.loads(page.get_by_test_id("lab-json").inner_text())

            def upload(mode):
                old = page.get_by_test_id("lab-recent").input_value()
                page.get_by_test_id("lab-image").set_input_files(str(image))
                page.get_by_test_id("lab-caption").fill(caption)
                page.get_by_test_id("lab-mode").select_option(mode)
                page.get_by_test_id("lab-submit").click()
                page.wait_for_function("old => document.getElementById('lab-recent').value !== old", arg=old, timeout=30_000)
                page.wait_for_function("""() => {
                    const t = document.getElementById('lab-status').textContent;
                    return !t.includes('queued') && !t.includes('running') && !t.includes('Uploading');
                }""", timeout=210_000)
                if page.get_by_test_id("lab-json").count() != 1:
                    self.fail("Live estimator did not produce a result: " + page.get_by_test_id("lab-status").inner_text())
                data = result()
                self.assertEqual(data["status"], "complete")
                self.assertIn(data["estimate"]["reconciliation_status"], {"matched", "reconciled_from_items", "partial_item_breakdown", "reconciliation_required"})
                self.assertTrue(data["estimate"]["items"])
                return data

            before = snapshot_domain()
            estimate = upload("estimate")
            self.assertNotIn("action", estimate)
            self.assertEqual(snapshot_domain(), before)
            page.screenshot(path=str(output_dir / "nutrition-lab-estimate-mobile.png"), full_page=True)

            pending = upload("log")
            self.assertEqual(pending["action"]["status"], "pending")
            pending_id = pending["job_id"]
            page.reload(wait_until="domcontentloaded")
            self._wait_for_app_ready(page)
            self._navigate(page, "nutrition", "#nutrition-view")
            page.get_by_test_id("lab-recent").select_option(pending_id)
            page.get_by_test_id("lab-correct-portion-smaller").wait_for(state="visible", timeout=30_000)
            page.get_by_test_id("lab-correct-portion-smaller").click()
            page.wait_for_function("""() => {
                const raw = document.querySelector('[data-testid="lab-json"]')?.textContent;
                if (!raw) return false;
                const job = JSON.parse(raw);
                return job.action.estimate.calories < job.action.original_estimate.calories;
            }""", timeout=30_000)
            corrected = result()
            page.get_by_test_id("lab-confirm").click()
            page.wait_for_function("""() => {
                const raw = document.querySelector('[data-testid="lab-json"]')?.textContent;
                return raw && JSON.parse(raw).recommendation_status === 'complete';
            }""", timeout=210_000)
            confirmed = result()
            self.assertEqual(confirmed["action"]["status"], "confirmed")
            self.assertIn("strategy_version", confirmed["recommendation"])
            day = context.request.get(self.base_url + "/api/nutrition/day").json()
            meal = next(item for item in day["meals"] if item["meal_id"] == confirmed["action"]["meal_id"])
            self.assertEqual(meal["macros"], corrected["action"]["estimate"]["total_best"])
            page.screenshot(path=str(output_dir / "nutrition-lab-confirmed-mobile.png"), full_page=True)
            for width in (360, 1280):
                page.set_viewport_size({"width": width, "height": 900})
                self._assert_no_horizontal_overflow(page)
            page.screenshot(path=str(output_dir / "nutrition-lab-desktop.png"), full_page=True)
            before_cancel = context.request.get(self.base_url + "/api/nutrition/day").json()
            upload("log")
            page.get_by_test_id("lab-cancel").click()
            page.wait_for_function("""() => {
                const raw = document.querySelector('[data-testid="lab-json"]')?.textContent;
                return raw && JSON.parse(raw).action?.status === 'cancelled';
            }""", timeout=30_000)
            after_cancel = context.request.get(self.base_url + "/api/nutrition/day").json()
            self.assertEqual(after_cancel["consumed"], before_cancel["consumed"])
            self.assertEqual(after_cancel["meal_count"], before_cancel["meal_count"])
            import json
            (output_dir / "nutrition-lab-live-results.json").write_text(json.dumps({
                "image": image.name, "caption": caption,
                "estimate_only": estimate, "corrected_confirmation": confirmed, "cancelled": result(),
            }, indent=2), encoding="utf-8")
            page.get_by_test_id("logout").click()
            page.get_by_test_id("browser-login-form").wait_for(state="visible", timeout=30_000)
            self.assertTrue(page.get_by_test_id("nutrition-lab").is_hidden())
            self.assertEqual(context.request.get(self.base_url + "/api/e2e/nutrition-lab/jobs").status, 401)
        finally:
            browser.close()
            playwright.stop()

    def test_live_screenshots(self):
        output_dir = Path("artifacts/e2e")
        output_dir.mkdir(parents=True, exist_ok=True)
        playwright, browser = self._new_browser()
        try:
            context = browser.new_context(viewport={"width": 360, "height": 800})
            page = self._open_page(context)
            self._login(page)
            self._assert_no_horizontal_overflow(page)
            page.screenshot(path=str(output_dir / "home-mobile.png"), full_page=True)
            self._check_nutrition_history(page)
            page.screenshot(path=str(output_dir / "nutrition-mobile.png"), full_page=True)
            self._navigate(page, "workout", "#workout-view")
            page.screenshot(path=str(output_dir / "workout-programme-mobile.png"), full_page=True)
            self._start_workout(page)
            page.screenshot(path=str(output_dir / "workout-active-mobile.png"), full_page=True)
            self._save_and_repeat_a_set(page)
            self._skip_all_exercises(page)
            self._complete_workout(page)
            page.screenshot(path=str(output_dir / "workout-complete-mobile.png"), full_page=True)

            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="domcontentloaded", timeout=45_000)
            self._wait_for_app_ready(page)
            self._assert_no_horizontal_overflow(page)

            page.set_viewport_size({"width": 1280, "height": 900})
            self._navigate(page, "profile", "#profile-view")
            self._assert_no_horizontal_overflow(page)
            page.screenshot(path=str(output_dir / "profile-desktop.png"), full_page=True)
            page.get_by_test_id("logout").click()
            page.get_by_test_id("browser-login-form").wait_for(state="visible", timeout=30_000)
        finally:
            browser.close()
            playwright.stop()


if __name__ == "__main__":
    unittest.main()
