"""Offline dashboard checks; all requests stay in an isolated fake account."""
import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tests import test_nutrition_lab as fixtures


class NutritionDashboardBrowserTests(unittest.TestCase):
    def test_compact_progress_history_and_responsive_layout(self):
        from playwright.sync_api import sync_playwright
        from lambda_handlers import api

        fixture = fixtures.LabTests()
        fixture.setUp()
        project = Path(__file__).resolve().parents[1]
        screenshots = project / "artifacts/e2e/compact-nutrition"
        screenshots.mkdir(parents=True, exist_ok=True)
        today = {
            "date": "2026-09-06", "today": "2026-09-06", "timezone": "Asia/Singapore",
            "target_effective_at": "2026-09-05T01:00:00+00:00", "meal_count": 1,
            "target": {"calories": 2750, "protein_g": 128, "carbs_g": 398, "fat_g": 72},
            "consumed": {"calories": 600.2, "protein_g": 40.2, "carbs_g": 60.3, "fat_g": 15.4},
            "meals": [{"meal_id": "synthetic-meal", "caption": "E2E baseline meal",
                       "eaten_at": "2026-09-06T03:23:00+00:00",
                       "macros": {"calories": 600.2, "protein_g": 40.2, "carbs_g": 60.3, "fat_g": 15.4}}],
        }
        yesterday = {**copy.deepcopy(today), "date": "2026-09-05", "target": None}
        errors = []
        try:
            with patch.object(api, "_service", return_value=fixture.service), sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page.clock.set_fixed_time(datetime(2026, 9, 6, 4, tzinfo=timezone.utc))
                page.on("pageerror", lambda error: errors.append(str(error)))

                def route_request(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.path == "/api/nutrition/day":
                        day = parse_qs(parsed.query).get("date", [today["date"]])[0]
                        route.fulfill(json=today if day == today["date"] else yesterday)
                    elif parsed.path.startswith("/api/"):
                        response = fixture.client.request(
                            request.method, parsed.path,
                            content=request.post_data_buffer,
                            headers={**request.headers, "Origin": "https://testserver"},
                        )
                        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
                    elif parsed.path in ("/", "/app.js", "/styles.css"):
                        file = project / "miniapp" / ("index.html" if parsed.path == "/" else parsed.path[1:])
                        route.fulfill(body=file.read_bytes(), content_type={"/": "text/html", "/app.js": "text/javascript", "/styles.css": "text/css"}[parsed.path])
                    else:
                        route.fulfill(status=404, body="")

                page.route("**/*", route_request)
                page.goto("https://testserver/")
                home = page.locator(".home-daily-summary-panel")
                home.get_by_role("progressbar").wait_for(state="visible")
                page.wait_for_function("!document.querySelector('#home-nutrition-refresh').disabled")
                self.assertIn("600 / 2,750 kcal", home.inner_text())
                self.assertEqual(page.locator("#home-view > :first-child").get_attribute("class"), home.get_attribute("class"))
                self.assertFalse(page.locator(".home-target-details").evaluate("el => el.open"))
                page.screenshot(path=str(screenshots / "home-mobile.png"))
                page.get_by_test_id("nav-nutrition").click()
                for width, height in ((390, 844), (360, 800), (1280, 900)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate("window.scrollTo(0, 0)")
                    nav_top = page.get_by_test_id("bottom-navigation").bounding_box()["y"]
                    for selector in ("nutrition-progress-fat_g", "nutrition-log-meal", "nutrition-meal-synthetic-meal"):
                        box = page.get_by_test_id(selector).bounding_box()
                        self.assertLess(box["y"], nav_top, f"{selector} is below the fold at {width}px")
                        if selector != "nutrition-meal-synthetic-meal":
                            self.assertLessEqual(box["y"] + box["height"], nav_top)
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    page.screenshot(path=str(screenshots / f"nutrition-{width}.png"))

                calories = page.get_by_test_id("nutrition-progress-calories")
                self.assertIn("2,150 kcal remaining", calories.inner_text())
                self.assertIn("40 / 128 g", page.get_by_test_id("nutrition-progress-protein_g").inner_text())
                self.assertEqual(page.get_by_test_id("nutrition-log-meal").get_attribute("href"), "https://t.me/javaanfitness_bot")
                self.assertTrue(page.locator("#nutrition-progress-meta").is_hidden())
                page.locator("#nutrition-target-details summary").click()
                self.assertIn("Effective from", page.locator("#nutrition-progress-meta").inner_text())
                page.locator("#nutrition-target-details summary").click()

                # A historical date without targets still shows its logged food;
                # browsing it must not replace Home's Today summary.
                page.get_by_test_id("nutrition-previous-day").click()
                page.wait_for_function("document.querySelector('#nutrition-progress-title').textContent === 'Logged nutrition'")
                self.assertIn("600 kcal", calories.inner_text())
                self.assertIn("/logmeal", page.locator("#nutrition-log-meal-hint").inner_text())
                self.assertEqual(calories.get_by_role("progressbar").count(), 0)
                page.get_by_test_id("nav-home").click()
                self.assertIn("600 / 2,750 kcal", home.inner_text())
                page.get_by_test_id("nav-nutrition").click()
                today["consumed"]["calories"] = 3000
                page.get_by_test_id("nutrition-next-day").click()
                page.wait_for_function("document.querySelector('[data-testid=nutrition-progress-calories]').textContent.includes('250 kcal over target')")
                self.assertEqual(calories.get_by_role("progressbar").get_attribute("value"), "2750")
                today["consumed"] = {key: 0 for key in today["consumed"]}
                today["meal_count"], today["meals"] = 0, []
                page.reload()
                page.locator("#nutrition-meals-empty").wait_for(state="visible")
                self.assertIn("0 / 2,750 kcal", calories.inner_text())
                self.assertTrue(page.get_by_test_id("nutrition-log-meal").is_visible())
                self.assertEqual(errors, [])
                browser.close()
        finally:
            fixture.doCleanups()
