"""Offline Chromium integration: real upload bytes, only cloud services stubbed."""
import asyncio
import json
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from tests import test_nutrition_lab as fixtures


class NutritionLabBrowserTests(unittest.TestCase):
    def test_browser_lab_controls_and_normal_account_visibility(self):
        from playwright.sync_api import sync_playwright
        from scripts.nutrition_corpus import case_by_id, image_path
        food_case = case_by_id("astons-all-day-breakfast-001")
        fixture = fixtures.LabTests()
        fixture.setUp()
        fixture.s3.get_object.side_effect = lambda **kw: {"Body": __import__("io").BytesIO(fixture.s3.put_object.call_args.kwargs["Body"])}
        fixture.service._planner._recommendation_client = None

        def invoke(**kwargs):
            event = json.loads(kwargs["Payload"])
            asyncio.run(fixture.lab.process(event["job_id"], event["operation"], estimator=fixture.estimator))
            return {"StatusCode": 202}

        fixture.invoker.invoke.side_effect = invoke
        from lambda_handlers import api
        from macro_bot import nutrition_lab
        project = Path(__file__).resolve().parents[1]
        try:
            with patch.object(api, "_service", return_value=fixture.service), patch.object(nutrition_lab, "NutritionLab", return_value=fixture.lab), sync_playwright() as pw:
                browser = pw.chromium.launch()
                context = browser.new_context(viewport={"width": 390, "height": 844})
                errors = []
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))

                def route_request(route):
                    request = route.request
                    path = urlsplit(request.url).path
                    if path.startswith("/api/"):
                        response = fixture.client.request(request.method, path + ("?" + urlsplit(request.url).query if urlsplit(request.url).query else ""),
                                                          content=request.post_data_buffer, headers={**request.headers, "Origin": "https://testserver"})
                        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
                    elif path in ("/", "/app.js", "/styles.css"):
                        file = project / "miniapp" / ("index.html" if path == "/" else path[1:])
                        route.fulfill(body=file.read_bytes(), content_type={"/": "text/html", "/app.js": "text/javascript", "/styles.css": "text/css"}[path])
                    else:
                        route.fulfill(status=404, body="")

                page.route("**/*", route_request)
                page.goto("https://testserver/#nutrition-lab")
                page.get_by_test_id("nutrition-lab").wait_for(state="visible")
                page.get_by_test_id("nutrition-lab-file").set_input_files(str(image_path(food_case)))
                page.get_by_test_id("nutrition-lab-caption").fill(food_case["caption"])
                page.get_by_test_id("nutrition-lab-run").click()
                page.get_by_test_id("lab-json").wait_for(state="visible")
                self.assertEqual(page.locator("#bottom-nav .nav-item").count(), 4)
                self.assertTrue(page.get_by_test_id("nutrition-lab-telegram-preview").is_visible())
                self.assertNotIn("action", json.loads(page.get_by_test_id("lab-json").inner_text()))
                page.get_by_test_id("nutrition-lab-mode").select_option("log")
                page.get_by_test_id("nutrition-lab-run").click()
                page.get_by_test_id("nutrition-lab-adjust").click()
                page.get_by_test_id("lab-correct-portion-smaller").click()
                page.get_by_test_id("nutrition-lab-confirm").click()
                page.wait_for_function("document.getElementById('lab-status').textContent.includes('Recommendation: complete')")
                self.assertIn("confirmed", page.get_by_test_id("lab-status").inner_text())
                for width in (360, 390, 1280):
                    page.set_viewport_size({"width": width, "height": 900})
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                Path("artifacts/e2e").mkdir(parents=True, exist_ok=True)
                page.screenshot(path="artifacts/e2e/nutrition-lab-offline-desktop.png", full_page=True)
                other = fixture.repo.resolve_identity(123, "real", "Real")
                token, _ = fixture.repo.create_browser_session(other)
                fixture.client.cookies.clear()
                fixture.client.cookies.set("jf_session", token)
                page.reload()
                page.wait_for_function("location.hash === '#home'")
                self.assertTrue(page.get_by_test_id("nutrition-lab").is_hidden())
                self.assertEqual(errors, [])
                browser.close()
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
