"""Offline browser regressions for daily nutrition navigation and refresh.

Serve the local Mini App with the real profile/auth API and controlled day reads.
No AWS, Telegram, or real-user records are accessed.
"""
import copy
import unittest
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tests import test_nutrition_lab as fixtures


class NutritionRefreshBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright, expect
        from lambda_handlers import api

        self.expect = expect
        self.fixture = fixtures.LabTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        service_patch = patch.object(api, "_service", return_value=self.fixture.service)
        service_patch.start()
        self.addCleanup(service_patch.stop)
        self.today = self.fixture.service.daily_nutrition_payload(self.fixture.identity)
        self.today["consumed"]["calories"] = 600
        self.today["meal_count"] = 1
        self.yesterday_date = "2026-09-04"
        self.yesterday = copy.deepcopy(self.today)
        self.yesterday.update(date=self.yesterday_date, target=None, meal_count=0)
        self.yesterday["consumed"]["calories"] = 0
        self.requests = []
        self.fail_reads = False
        self.hold_next = False
        self.held = []
        self.errors = []
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch()
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width": 390, "height": 844})
        self.page.clock.set_fixed_time(self.fixture.now)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", self._route)
        self.addCleanup(self._release_held)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def _release_held(self):
        while self.held:
            route, payload = self.held.pop()
            route.fulfill(json=payload)

    def _route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.path == "/api/nutrition/day":
            date = parse_qs(parsed.query).get("date", [""])[0]
            self.requests.append(date)
            payload = copy.deepcopy(self.yesterday if date == self.yesterday_date else self.today)
            if self.hold_next:
                self.hold_next = False
                self.held.append((route, payload))
            elif self.fail_reads:
                route.fulfill(status=503, json={"detail": "Nutrition temporarily unavailable."})
            else:
                route.fulfill(json=payload)
        elif parsed.path.startswith("/api/"):
            response = self.fixture.client.request(
                request.method, parsed.path + ("?" + parsed.query if parsed.query else ""),
                content=request.post_data_buffer,
                headers={**request.headers, "Origin": "https://testserver"},
            )
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
        elif parsed.path in ("/", "/app.js", "/styles.css"):
            project = Path(__file__).resolve().parents[1]
            file = project / "miniapp" / ("index.html" if parsed.path == "/" else parsed.path[1:])
            route.fulfill(body=file.read_bytes(), content_type={
                "/": "text/html", "/app.js": "text/javascript", "/styles.css": "text/css",
            }[parsed.path])
        else:
            route.fulfill(status=404, body="")

    def _open(self):
        self.page.goto("https://testserver/")
        self.page.locator("#app-shell").wait_for(state="visible")
        self.expect(self.page.get_by_test_id("home-nutrition-refresh")).to_be_enabled()

    def _navigate(self, view):
        self.page.get_by_test_id(f"nav-{view}").click()
        self.page.locator(f"#{view}-view").wait_for(state="visible")

    def _home_calories(self, value):
        self.expect(self.page.locator("#home-daily-summary-macros")).to_contain_text(value)

    def _nutrition_calories(self, value):
        self.expect(self.page.get_by_test_id("nutrition-progress-calories")).to_contain_text(value)

    def test_history_keeps_home_today_independent_and_preserves_selected_date(self):
        self._open()
        self._home_calories("600")
        for has_target in (False, True):
            with self.subTest(historical_target=has_target):
                self.yesterday["target"] = self.today["target"] if has_target else None
                self._navigate("nutrition")
                self.expect(self.page.get_by_test_id("nutrition-refresh")).to_be_enabled()
                self.page.get_by_test_id("nutrition-previous-day").click()
                self.expect(self.page.locator("#nutrition-date-label")).to_contain_text("Sep 4")
                self._navigate("home")
                self._home_calories("600")
                self.expect(self.page.locator("#home-daily-summary-title")).to_have_text("Today")
                self.expect(self.page.get_by_test_id("home-nutrition-refresh")).to_be_enabled()
                self.assertEqual(self.requests[-1], "")
                self._navigate("nutrition")
                self.expect(self.page.get_by_test_id("nutrition-refresh")).to_be_enabled()
                self.expect(self.page.locator("#nutrition-date-label")).to_contain_text("Sep 4")
                self.assertEqual(self.requests[-1], self.yesterday_date)
                self.page.get_by_test_id("nutrition-next-day").click()
                self.expect(self.page.locator("#nutrition-day-title")).to_have_text("Today")
                self._navigate("home")

    def test_returning_to_view_and_app_refreshes_newly_logged_totals(self):
        self._open()
        self._navigate("profile")
        self.today["consumed"]["calories"] = 900
        self._navigate("nutrition")
        self._nutrition_calories("900")
        self.today["consumed"]["calories"] = 1000
        self.page.evaluate("window.dispatchEvent(new Event('focus'))")
        self._nutrition_calories("1,000")
        self.today["consumed"]["calories"] = 1100
        self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        self._nutrition_calories("1,100")
        self.today["consumed"]["calories"] = 1200
        self._navigate("home")
        self._home_calories("1,200")

    def test_failed_refresh_preserves_totals_and_retry_recovers_in_both_views(self):
        self._open()
        for view, button_id, status_id in (
            ("home", "home-nutrition-refresh", "home-nutrition-status"),
            ("nutrition", "nutrition-refresh", "nutrition-refresh-status"),
        ):
            with self.subTest(view=view):
                self._navigate(view)
                button = self.page.get_by_test_id(button_id)
                self.expect(button).to_be_enabled()
                self.fail_reads = True
                button.click()
                self.expect(button).to_have_text("Retry")
                self.expect(self.page.locator(f"#{status_id}")).to_contain_text("Showing last loaded totals")
                (self._home_calories if view == "home" else self._nutrition_calories)("600")
                self.fail_reads = False
                button.click()
                self.expect(button).to_have_text("Refresh")
                self.expect(self.page.locator(f"#{status_id}")).to_be_hidden()

    def test_initial_read_failure_can_be_retried_without_reloading_app(self):
        self.fail_reads = True
        self._open()
        self.expect(self.page.get_by_test_id("home-nutrition-refresh")).to_have_text("Retry")
        self._navigate("nutrition")
        self.expect(self.page.get_by_test_id("nutrition-refresh")).to_have_text("Retry")
        self.fail_reads = False
        self.page.get_by_test_id("nutrition-refresh").click()
        self._nutrition_calories("600")
        self._navigate("home")
        self._home_calories("600")

    def test_slow_history_response_does_not_replace_home_or_duplicate_reads(self):
        self._open()
        self._navigate("nutrition")
        self.expect(self.page.get_by_test_id("nutrition-refresh")).to_be_enabled()
        self.hold_next = True
        self.page.get_by_test_id("nutrition-previous-day").click()
        self.expect(self.page.get_by_test_id("nutrition-refresh")).to_have_text("Refreshing…")
        self.today["consumed"]["calories"] = 900
        self._navigate("home")
        self._home_calories("900")
        route, payload = self.held.pop()
        route.fulfill(json=payload)
        self.expect(self.page.get_by_test_id("nutrition-refresh")).to_be_enabled()
        self._home_calories("900")
        self.hold_next = True
        before = len(self.requests)
        self.page.get_by_test_id("home-nutrition-refresh").click()
        self.page.evaluate("window.dispatchEvent(new Event('focus')); document.dispatchEvent(new Event('visibilitychange'))")
        self.assertEqual(len(self.requests), before + 1)
        self.expect(self.page.get_by_test_id("home-nutrition-refresh")).to_be_disabled()
        route, payload = self.held.pop()
        route.fulfill(json=payload)
        self.expect(self.page.get_by_test_id("home-nutrition-refresh")).to_be_enabled()

    def test_today_rolls_over_at_local_midnight_on_return(self):
        self._open()
        self._navigate("nutrition")
        self._nutrition_calories("600")
        self.page.clock.set_fixed_time(self.fixture.now + timedelta(days=1))
        self.today.update(date="2026-09-06", today="2026-09-06", meal_count=0)
        self.today["consumed"]["calories"] = 0
        self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        self.expect(self.page.locator("#nutrition-date-label")).to_contain_text("Sep 6")
        self.expect(self.page.locator("#nutrition-day-title")).to_have_text("Today")
        self._navigate("home")
        self.expect(self.page.locator("#home-daily-summary-meta")).to_have_text("0 meals logged")
        self.expect(self.page.locator("#home-daily-summary-macros")).not_to_contain_text("600")
