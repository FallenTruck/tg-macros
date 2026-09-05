"""Offline browser integration against the real API and an isolated fake repository."""
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from tests import test_nutrition_lab as fixtures
from e2e.profile_flow import exercise_profile_settings


class ProfileBrowserTests(unittest.TestCase):
    def test_profile_settings_roundtrip_and_target_recalculation(self):
        from playwright.sync_api import sync_playwright
        from lambda_handlers import api
        fixture = fixtures.LabTests()
        fixture.setUp()
        project = Path(__file__).resolve().parents[1]
        try:
            with patch.object(api, '_service', return_value=fixture.service), sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def route_request(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.path.startswith('/api/'):
                        response = fixture.client.request(request.method, parsed.path + ('?' + parsed.query if parsed.query else ''),
                            content=request.post_data_buffer, headers={**request.headers, 'Origin': 'https://testserver'})
                        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
                    elif parsed.path in ('/', '/app.js', '/styles.css'):
                        file = project / 'miniapp' / ('index.html' if parsed.path == '/' else parsed.path[1:])
                        route.fulfill(body=file.read_bytes(), content_type={'/': 'text/html', '/app.js': 'text/javascript', '/styles.css': 'text/css'}[parsed.path])
                    else:
                        route.fulfill(status=404, body='')
                page.route('**/*', route_request)
                page.goto('https://testserver/#profile')
                page.locator('#nutrition-settings-form').wait_for(state='visible')
                exercise_profile_settings(page, 'artifacts/e2e/profile-offline')
                saved = fixture.repo.get_profile(fixture.identity.user_id)
                self.assertEqual(saved.recommendation_bedtime, '22:45')
                self.assertEqual(saved.forbidden_ingredients, ['peanut'])
                self.assertEqual(saved.questionnaire_answers.weight_kg, 81)
                self.assertEqual(errors, [])
                browser.close()
        finally:
            fixture.doCleanups()
