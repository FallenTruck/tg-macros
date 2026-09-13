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
                # Saved sets can be corrected even after the exercise is complete.
                # Keep the next-set draft separate from the edit form.
                first.locator('form input[name="reps"]').fill('12')
                row = first.locator('.workout-set-row').nth(1)
                row.get_by_role('button', name='Edit set 2').click()
                edit = first.locator('[data-edit-set]')
                expect(edit.locator('input[name="load_value"]')).to_have_value('25')
                expect(edit.locator('input[name="reps"]')).to_have_value('8')
                edit.locator('input[name="reps"]').fill('11')
                # The editor replaces the same row, without an extra section.
                expect(row).to_have_attribute('data-edit-set', '')
                expect(first.locator('.workout-set-row')).to_have_count(3)
                output = project / 'artifacts/e2e/set-edit-inline'
                output.mkdir(parents=True, exist_ok=True)
                page.set_viewport_size({'width': 360, 'height': 800})
                edit.evaluate("form => form.scrollIntoView({block: 'center'})")
                edit.screenshot(path=str(output / 'editing-row.png'))
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
                edit.get_by_role('button', name='Cancel edit').click()
                expect(first.locator('[data-edit-set]')).to_have_count(0)
                expect(row).to_contain_text('25 kg × 8')
                row.get_by_role('button', name='Edit set 2').click()
                expect(edit.locator('input[name="reps"]')).to_have_value('8')
                edit.locator('input[name="reps"]').fill('10')
                edit.get_by_role('button', name='Save changes').click()
                expect(first.locator('[data-edit-set]')).to_have_count(0)
                expect(row).to_contain_text('25 kg × 10')
                expect(first.locator('form input[name="reps"]')).to_have_value('12')
                expect(first.locator('.workout-set-row')).to_have_count(3)

                # Another device wins: reject stale edits and show its saved values.
                row.get_by_role('button', name='Edit set 2').click()
                edit.locator('input[name="reps"]').fill('13')
                current = fixture.service.active_workout(user)
                execution = next(ex for ex in current['executions'] if ex['execution_id'] == eid)
                saved = next(item for item in execution['sets'] if item['set_ordinal'] == 2)
                fixture.service.put_workout_set(user, sid, eid, 2,
                    {'load_value': 25, 'reps': 9, 'expected_revision': saved['revision'],
                     'execution_expected_revision': execution['revision']})
                edit.get_by_role('button', name='Save changes').click()
                expect(edit).to_have_count(0)
                expect(row).to_contain_text('25 kg × 9')
                self.assertEqual(writes[-1][1], 409)

                # A lost edit response reconciles without duplicating the set.
                row.get_by_role('button', name='Edit set 2').click()
                edit.locator('input[name="reps"]').fill('10')
                faults['lose_save'] = True
                edit.get_by_role('button', name='Save changes').click()
                expect(edit).to_have_count(0)
                expect(row).to_contain_text('25 kg × 10')
                expect(first.locator('.workout-set-row')).to_have_count(3)
                page.reload()
                page.locator('[data-route="workout"]').click()
                expect(row).to_contain_text('25 kg × 10')

                # Submission locks both the UI and existing-set API updates.
                current = fixture.service.active_workout(user)
                for execution in current['executions'][1:]:
                    fixture.service.skip_workout_exercise(user, sid, execution['execution_id'],
                        {'expected_revision': execution['revision']})
                page.reload()
                page.locator('[data-route="workout"]').click()
                row.get_by_role('button', name='Edit set 2').click()
                expect(page.get_by_test_id('submit-workout')).to_be_disabled()
                edit.get_by_role('button', name='Cancel edit').click()
                page.get_by_test_id('submit-workout').click()
                expect(page.get_by_test_id('active-workout')).to_have_count(0)
                expect(page.locator('[data-action="edit-set"]')).to_have_count(0)
                before = fixture.service.workout_session(user, sid)
                execution = next(ex for ex in before['executions'] if ex['execution_id'] == eid)
                saved = next(item for item in execution['sets'] if item['set_ordinal'] == 2)
                response = fixture.client.put(f'/api/workout/sessions/{sid}/executions/{eid}/sets/2',
                    json={'load_value': 25, 'reps': 99, 'expected_revision': saved['revision'],
                          'execution_expected_revision': execution['revision']}, headers={'Origin': 'https://testserver'})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(fixture.service.workout_session(user, sid), before)
                self.assertEqual(errors, [])
                browser.close()
        finally:
            fixture.doCleanups()
