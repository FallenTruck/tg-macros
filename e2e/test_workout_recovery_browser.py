"""Offline browser regressions using local assets and the real API with fake storage."""
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from tests import test_nutrition_lab as fixtures


class WorkoutRecoveryBrowserTests(unittest.TestCase):
    def test_drafts_conflicts_uncertain_saves_and_pending_guard(self):
        from playwright.sync_api import sync_playwright, expect
        from lambda_handlers import api
        fixture = fixtures.LabTests()
        fixture.setUp()
        fixture.repo.seed_workout_programme()
        project = Path(__file__).resolve().parents[1]
        faults = {'lose_save': False, 'lose_refresh': False}
        writes = []
        errors = []
        try:
            with patch.object(api, '_service', return_value=fixture.service), sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                page.on('pageerror', lambda error: errors.append(str(error)))

                def route_request(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.path.startswith('/api/'):
                        if parsed.path == '/api/workout/sessions/active' and faults['lose_refresh']:
                            route.abort('failed')
                            return
                        response = fixture.client.request(request.method, parsed.path,
                            content=request.post_data_buffer, headers={**request.headers, 'Origin': 'https://testserver'})
                        if request.method == 'PUT' and '/sets/' in parsed.path:
                            writes.append((parsed.path, response.status_code))
                            if faults['lose_save']:
                                faults['lose_save'] = False
                                self.assertEqual(response.status_code, 200)
                                route.abort('failed')
                                return
                        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
                    elif parsed.path in ('/', '/app.js', '/styles.css'):
                        file = project / 'miniapp' / ('index.html' if parsed.path == '/' else parsed.path[1:])
                        route.fulfill(body=file.read_bytes(), content_type={'/': 'text/html', '/app.js': 'text/javascript', '/styles.css': 'text/css'}[parsed.path])
                    else:
                        route.fulfill(status=404, body='')

                page.route('**/*', route_request)
                page.goto('https://testserver/')
                page.get_by_test_id('bottom-navigation').wait_for(state='visible')
                page.locator('[data-route="workout"]').click()
                page.get_by_test_id('workout-day-start-PULL').click()
                cards = page.locator('article[data-testid^="workout-execution-"]')
                first, second = cards.nth(0), cards.nth(1)
                expect(first).to_be_visible()
                for card, load, reps in ((first, '20', '8'), (second, '30', '10')):
                    card.locator('input[name="load_value"]').fill(load)
                    card.locator('input[name="reps"]').fill(reps)
                # Two events in the same tick must produce a single mutation.
                first.locator('form').evaluate('(form) => { form.requestSubmit(); form.requestSubmit(); }')
                expect(first.locator('.workout-set-row')).to_have_count(1)
                self.assertEqual(len(writes), 1)
                expect(second.locator('input[name="load_value"]')).to_have_value('30')
                expect(second.locator('input[name="reps"]')).to_have_value('10')

                # Simulate a second device editing the previous set.
                form = first.locator('form')
                sid = form.get_attribute('data-session-id')
                eid = form.get_attribute('data-execution-id')
                user = fixture.identity
                fixture.service.put_workout_set(user, sid, eid, 1,
                    {'load_value': 22, 'reps': 9, 'expected_revision': 1, 'execution_expected_revision': 2})
                first.locator('input[name="load_value"]').fill('25')
                first.locator('input[name="reps"]').fill('8')
                first.get_by_test_id('workout-save-set').click()
                expect(first.locator('form')).to_have_attribute('data-execution-revision', '3')
                self.assertEqual(writes[-1][1], 409)
                expect(first.locator('input[name="load_value"]')).to_have_value('25')
                first.get_by_test_id('workout-save-set').click()
                expect(first.locator('.workout-set-row')).to_have_count(2)

                # A committed save whose response is lost must be recovered, not repeated.
                first.locator('input[name="load_value"]').fill('27')
                first.locator('input[name="reps"]').fill('8')
                faults['lose_save'] = True
                first.get_by_test_id('workout-save-set').click()
                expect(first.locator('.workout-set-row')).to_have_count(3)
                expect(first.locator('form')).to_have_attribute('data-ordinal', '4')
                expect(second.locator('input[name="reps"]')).to_have_value('10')

                # When refresh also fails, the next attempt only reconciles.
                second.locator('input[name="reps"]').fill('11')
                faults.update(lose_save=True, lose_refresh=True)
                second.get_by_test_id('workout-save-set').click()
                expect(second.get_by_test_id('workout-save-set')).to_be_enabled()
                expect(page.locator('#status-message')).to_contain_text('Could not verify')
                count = len(writes)
                faults['lose_refresh'] = False
                second.get_by_test_id('workout-save-set').click()
                expect(second.locator('.workout-set-row')).to_have_count(1)
                expect(second.locator('form')).to_have_attribute('data-ordinal', '2')
                self.assertEqual(len(writes), count)
                self.assertEqual(errors, [])
                browser.close()
        finally:
            fixture.doCleanups()
