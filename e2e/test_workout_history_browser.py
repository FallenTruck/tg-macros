"""Offline Chromium history flow against the real API and synthetic storage."""
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
from tests import test_nutrition_lab as fixtures
from tests.test_workout_history import HistoryTable


class BrowserHistoryTable(HistoryTable, fixtures.LabTable):
    pass


class WorkoutHistoryBrowserTests(unittest.TestCase):
    def test_history_pagination_read_only_empty_and_failure(self):
        from playwright.sync_api import sync_playwright, expect
        from lambda_handlers import api
        fixture = fixtures.LabTests()
        fixture.setUp()
        fixture.repo.seed_workout_programme()
        fixture.table.__class__ = BrowserHistoryTable
        project = Path(__file__).resolve().parents[1]
        failure = False
        try:
            with patch.object(api, "_service", return_value=fixture.service), sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={"width": 390, "height": 844})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                def route_request(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.path == "/api/workout/history" and failure:
                        route.fulfill(status=500, content_type="application/json", body='{"detail":"History unavailable"}')
                    elif parsed.path.startswith("/api/"):
                        response = fixture.client.request(request.method, parsed.path + ("?" + parsed.query if parsed.query else ""),
                            content=request.post_data_buffer, headers={**request.headers, "Origin": "https://testserver"})
                        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
                    elif parsed.path in ("/", "/app.js", "/styles.css"):
                        file = project / "miniapp" / ("index.html" if parsed.path == "/" else parsed.path[1:])
                        route.fulfill(body=file.read_bytes(), content_type={"/": "text/html", "/app.js": "text/javascript", "/styles.css": "text/css"}[parsed.path])
                    else:
                        route.fulfill(status=404, body="")
                page.route("**/*", route_request)
                page.goto("https://testserver/")
                page.get_by_test_id("bottom-navigation").wait_for(state="visible")
                page.locator('[data-route="workout"]').click()
                page.locator('#open-workout-history').click()
                history = page.locator('#workout-history')
                expect(history).to_contain_text("No workout history yet")
                for index in range(21):
                    fixture.service.workout_execution.session_id_factory = lambda i=index: f"history-{i:03d}"
                    payload = fixture.service.start_workout(fixture.identity, "PULL")
                    sid = payload["session"]["session_id"]
                    if index == 0:
                        for ex in payload["executions"]:
                            fixture.service.skip_workout_exercise(fixture.identity, sid, ex["execution_id"], {"expected_revision": 1})
                        fixture.service.complete_workout(fixture.identity, sid, {"expected_revision": 1})
                    else:
                        fixture.service.put_workout_set(fixture.identity, sid, payload["executions"][0]["execution_id"], 1, {"load_value": 40, "reps": 8, "rir": 2})
                        fixture.service.cancel_workout(fixture.identity, sid, {"expected_revision": 1})
                # Keep a live workout too: its completion dock must be hidden in history.
                fixture.service.workout_execution.session_id_factory = lambda: "still-active"
                fixture.service.start_workout(fixture.identity, "PULL")
                page.reload()
                page.locator('[data-route="workout"]').click()
                page.locator('#open-workout-history').click()
                expect(history.locator('.workout-history-row')).to_have_count(20)
                expect(history).to_contain_text("Cancelled")
                history.get_by_role('button', name='Load More').click()
                expect(history.locator('.workout-history-row')).to_have_count(21)
                expect(history).to_contain_text("Completed")
                expect(history.get_by_role('button', name='Load More')).to_have_count(0)
                history.locator('.workout-history-row').first.click()
                expect(history).to_contain_text("Read-only")
                expect(history).to_contain_text("40 kg × 8")
                expect(history).to_contain_text("RIR 2")
                expect(history.locator('input, select, form, [data-action]')).to_have_count(0)
                expect(page.locator('#workout-completion-dock')).to_be_hidden()
                expect(page.locator('#workout-session')).to_be_hidden()
                output = project / "artifacts/e2e/workout-history"
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / "detail.png"), full_page=True)
                history.get_by_role('button', name='Back to history').click()
                expect(history.locator('.workout-history-row')).to_have_count(21)
                failure = True
                page.locator('#open-workout-history').click()
                expect(history.locator('[role="alert"]')).to_contain_text('History unavailable')
                failure = False
                history.get_by_role('button', name='Retry').click()
                expect(history.locator('.workout-history-row')).to_have_count(20)
                self.assertEqual(errors, [])
                browser.close()
        finally:
            fixture.doCleanups()
