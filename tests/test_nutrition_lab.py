import asyncio
import base64
import copy
import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from lambda_handlers import api, worker
from macro_bot.direct_estimator import DirectOpenAIEstimator
from macro_bot.nutrition_lab import NutritionLab, LabUnavailable, LabConflict, require_identity, validate_image, USER_PK
from macro_bot.serverless_auth import hash_web_password
from macro_bot.serverless_data import DynamoNutritionRepository, ActionFinalized
from macro_bot.serverless_service import NutritionService
from scripts.e2e_support import identity_item, marker_item, BASELINE_PROFILE_PAYLOAD
from tests.test_browser_auth import _BrowserFakeTable
from tests.test_direct_estimator import _Responses, _structured_payload, _jpg_bytes
from tests.test_serverless_data import ConditionalCheckFailedException
from tests.test_serverless_adapters import _FakeBot

ENV = {"ENVIRONMENT": "dev", "STACK_NAME": "tg-macros-dev", "AWS_REGION": "ap-southeast-1",
       "E2E_NUTRITION_LAB_ENABLED": "true", "E2E_NUTRITION_LAB_BUCKET": "lab-images",
       "E2E_NUTRITION_LAB_FUNCTION": "lab-worker", "MINI_APP_URL": "https://testserver"}
ROOT = "/api/e2e/nutrition-lab/jobs"


class LabTable(_BrowserFakeTable):
    """Extend the repository's fake to enforce Lab's compare-and-swap claims."""
    def _lab_condition(self, kwargs):
        key = kwargs.get("Key", kwargs.get("Item"))
        current = self.items.get((key["PK"], key["SK"]), {})
        expression = kwargs.get("ConditionExpression", "")
        values = kwargs.get("ExpressionAttributeValues", {})
        if expression == "#status = :expected" and current.get("status") != values[":expected"]:
            raise ConditionalCheckFailedException()
        if expression == "attribute_not_exists(recommendation_status)" and "recommendation_status" in current:
            raise ConditionalCheckFailedException()
        if expression.startswith("recommendation_status = "):
            if current.get("recommendation_status") != values[expression.split(" = ")[1]]:
                raise ConditionalCheckFailedException()

    def put_item(self, **kwargs):
        self._lab_condition(kwargs)
        return super().put_item(**kwargs)

    def update_item(self, **kwargs):
        self._lab_condition(kwargs)
        return super().update_item(**kwargs)


class LabTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = datetime(2026, 9, 5, 9, tzinfo=timezone.utc)
        self.table = LabTable()
        self.repo = DynamoNutritionRepository(self.table, table_name="fitness", now_fn=lambda: self.now)
        self.table.put_item(Item=identity_item())
        self.table.put_item(Item=marker_item())
        self.identity = self.repo.get_identity_by_key(identity_item()["PK"])
        self.repo.save_web_credential("javaan-e2e", identity=self.identity, password_record=hash_web_password("test-password"))
        self.service = NutritionService(self.repo)
        self.service.save_profile(self.identity, BASELINE_PROFILE_PAYLOAD)
        self.s3 = Mock()
        self.s3.get_object.side_effect = lambda **kw: {"Body": io.BytesIO(_jpg_bytes())}
        self.invoker = Mock()
        self.invoker.invoke.return_value = {"StatusCode": 202}
        self.lab = NutritionLab(self.service, s3=self.s3, lambda_client=self.invoker)
        self.responses = _Responses(_structured_payload())
        self.estimator = DirectOpenAIEstimator(client=Mock(responses=self.responses), max_retries=0)
        self.job_id = "a" * 32
        self.client = TestClient(api.app, base_url="https://testserver")
        token, _session = self.repo.create_browser_session(self.identity)
        self.client.cookies.set("jf_session", token)
        self.headers = {"Origin": "https://testserver"}

    def run_job(self, mode="estimate", job_id=None):
        job_id = job_id or self.job_id
        self.lab.submit(job_id, _jpg_bytes(), "half rice", mode)
        asyncio.run(self.lab.process(job_id, "analyze", estimator=self.estimator))
        return self.lab.response(job_id)

    def test_estimate_only_has_no_domain_writes_and_cleans_image(self):
        before = copy.deepcopy(self.table.items)
        job = self.run_job()
        changed = {key for key, item in self.table.items.items() if before.get(key) != item}
        self.assertEqual(changed, {(USER_PK, "LAB_JOB#" + self.job_id)})
        self.assertEqual(job["status"], "complete")
        self.assertNotIn("action", job)
        self.assertEqual(job["estimate"]["reconciliation_status"], "matched")
        self.assertTrue(self.responses.calls[0]["text"]["format"]["strict"])
        self.s3.delete_object.assert_called_once()

    def test_full_log_correction_confirm_recommendation_is_partition_bound(self):
        before = copy.deepcopy(self.table.items)
        job = self.run_job("log")
        original = job["action"]["estimate"]["calories"]
        self.assertEqual(self.service.daily_nutrition_payload(self.identity)["meal_count"], 0)
        job = self.lab.mutate(self.job_id, "correct", {"type": "portion", "value": "smaller"})
        corrected = job["action"]["estimate"]["calories"]
        self.assertLess(corrected, original)
        self.assertEqual(job["action"]["original_estimate"]["calories"], original)
        job = self.lab.mutate(self.job_id, "confirm", {})
        self.assertEqual(job["action"]["status"], "confirmed")
        self.assertEqual(self.service.daily_nutrition_payload(self.identity)["consumed"]["calories"], corrected)
        with patch.object(self.repo, "resolve_identity", side_effect=AssertionError("must use canonical identity")):
            self.service._planner._recommendation_client = None
            asyncio.run(self.lab.process(self.job_id, "recommend"))
        job = self.lab.response(self.job_id)
        self.assertEqual(job["recommendation_status"], "complete")
        self.assertEqual(job["recommendation"]["today_totals"]["calories"], corrected)
        self.assertEqual(self.repo.list_corrections(self.identity)[0]["final_status"], "confirmed")
        self.lab.mutate(self.job_id, "confirm", {})
        self.assertEqual(self.service.daily_nutrition_payload(self.identity)["meal_count"], 1)
        self.assertTrue(all(key[0] == USER_PK for key, item in self.table.items.items() if before.get(key) != item))
        self.assertNotIn(("IDENTITY#TELEGRAM#0", "USER"), self.table.items)
        with self.assertRaises(ActionFinalized):
            self.lab.mutate(self.job_id, "correct", {"type": "portion", "value": "smaller"})

    def test_cancel_and_duplicate_delivery_do_not_log_or_reestimate(self):
        self.run_job("log")
        asyncio.run(self.lab.process(self.job_id, "analyze", estimator=self.estimator))
        self.lab.submit(self.job_id, _jpg_bytes(), "half rice", "log")
        self.assertEqual(len(self.responses.calls), 1)
        self.lab.mutate(self.job_id, "cancel", {})
        self.lab.mutate(self.job_id, "confirm", {})
        self.assertEqual(self.lab.response(self.job_id)["action"]["status"], "cancelled")
        self.assertEqual(self.service.daily_nutrition_payload(self.identity)["meal_count"], 0)
        self.assertNotIn("recommendation_status", self.lab.read(self.job_id))

    def test_pending_meal_survives_adapter_restart_and_auto_confirms(self):
        self.run_job("log")
        other = NutritionLab(NutritionService(self.repo), s3=self.s3, lambda_client=self.invoker)
        action = other.response(self.job_id)["action"]
        self.now = self.now + timedelta(minutes=61)
        self.service.auto_confirm_expired_action(self.identity, action["token"])
        self.assertEqual(other.response(self.job_id)["action"]["status"], "confirmed")
        self.assertEqual(self.service.daily_nutrition_payload(self.identity)["meal_count"], 1)

    def test_invalid_model_output_fails_without_meal_and_deletes_image(self):
        self.responses.payload = {"secret": "not stored"}
        job = self.run_job("log")
        self.assertEqual(job["status"], "failed")
        self.assertNotIn("action", job)
        self.assertNotIn("secret", str(job))
        self.s3.delete_object.assert_called_once()

    def test_concurrent_worker_claim_runs_estimator_once(self):
        self.lab.submit(self.job_id, _jpg_bytes(), "", "log")
        async def concurrent():
            await asyncio.gather(*(self.lab.process(self.job_id, "analyze", estimator=self.estimator) for _ in range(2)))
        asyncio.run(concurrent())
        self.assertEqual(len(self.responses.calls), 1)
        actions = [item for item in self.table.items.values() if item.get("entity_type") == "meal_action"]
        self.assertEqual(len(actions), 1)

    def test_expired_request_id_cannot_recurse_or_recreate_a_job(self):
        self.run_job()
        self.now += timedelta(days=2)
        with self.assertRaises(LabConflict):
            self.lab.submit(self.job_id, _jpg_bytes(), "half rice", "estimate")
        self.assertEqual(len(self.responses.calls), 1)

    def test_upload_validation_and_conflicting_request_id(self):
        for image in (b"", b"not image", b"x" * 3_000_001):
            with self.assertRaises(ValueError):
                validate_image(image)
        self.run_job()
        with self.assertRaises(LabConflict):
            self.lab.submit(self.job_id, _jpg_bytes(), "changed", "estimate")

    def test_telegram_and_lab_use_same_analysis_method_and_prompt(self):
        # Actual adapters + actual service + actual estimator, stub only external OpenAI response.
        self.run_job("log")
        other = self.repo.resolve_identity(123, "real", "Real")
        bot = _FakeBot(_jpg_bytes())
        bot.image_bytes = _jpg_bytes()
        asyncio.run(worker._handle_photo(self.service, other, bot,
                    {"chat": {"id": 123}, "message_id": 44, "caption": "half rice", "photo": [{"file_id": "photo"}]},
                    11, self.estimator))
        self.assertEqual(self.responses.calls[0], self.responses.calls[1])
        telegram_action = self.repo.find_action_for_update(other, 11)
        self.assertEqual(telegram_action.estimate.to_payload(), self.lab.response(self.job_id)["estimate"])

    def test_worker_iam_uses_canonical_credential_key_and_reserved_write_partition(self):
        from pathlib import Path
        from macro_bot.serverless_auth import web_credential_key
        template = (Path(__file__).parents[1] / "template.yaml").read_text()
        role = template.split("  NutritionLabFunction:", 1)[1].split("  NutritionLabLogGroup:", 1)[0]
        self.assertIn(web_credential_key("javaan-e2e"), role)
        writes = role.split("Action: [dynamodb:PutItem", 1)[1].split("- Effect: Allow", 1)[0]
        self.assertIn("dynamodb:LeadingKeys: [USER#e2e-javaan-e2e]", writes)

    def test_deployment_gate_fails_closed(self):
        for key, bad in (("ENVIRONMENT", "prod"), ("STACK_NAME", "other-dev"), ("AWS_REGION", "us-east-1"), ("E2E_NUTRITION_LAB_ENABLED", "false")):
            with self.subTest(key=key), patch.dict(os.environ, {key: bad}):
                with self.assertRaises(LabUnavailable):
                    require_identity(self.repo, self.identity)

    def test_every_marker_identity_and_credential_mapping_is_validated(self):
        keys = [key for key in self.table.items if not key[0].startswith("USER#") and not key[0].startswith("WEB_SESSION#")]
        for key in keys:
            for field in ("user_id", "username", "telegram_user_id", "entity_type"):
                with self.subTest(key=key, field=field):
                    original = copy.deepcopy(self.table.items[key])
                    self.table.items[key][field] = "wrong"
                    with self.assertRaises(LabUnavailable):
                        require_identity(self.repo, self.identity)
                    self.table.items[key] = original
        key = ("E2E_ACCOUNT#javaan-e2e", "META")
        self.table.items.pop(key)
        with self.assertRaises(LabUnavailable):
            require_identity(self.repo)

    def test_http_security_on_every_route_including_forged_identity(self):
        self.run_job("log")
        routes = [("GET", ROOT), ("GET", ROOT + "/" + self.job_id), ("PUT", ROOT + "/" + "b" * 32),
                  ("POST", ROOT + "/" + self.job_id + "/correct"), ("POST", ROOT + "/" + self.job_id + "/confirm"),
                  ("POST", ROOT + "/" + self.job_id + "/cancel")]
        other = self.repo.resolve_identity(123, "javaan-e2e", "Fake synthetic name")
        real_token, _ = self.repo.create_browser_session(other)
        for scenario in ("anonymous", "real", "telegram", "disabled"):
            for method, url in routes:
                with self.subTest(scenario=scenario, method=method, url=url), patch.object(api, "_service", return_value=self.service):
                    cookies = dict(self.client.cookies)
                    self.client.cookies.clear()
                    headers = dict(self.headers)
                    if scenario == "real": self.client.cookies.set("jf_session", real_token)
                    if scenario == "telegram": headers["X-Telegram-Init-Data"] = "forged"
                    if scenario == "disabled": self.client.cookies.update(cookies)
                    with patch.dict(os.environ, {"E2E_NUTRITION_LAB_ENABLED": "false" if scenario == "disabled" else "true"}):
                        response = self.client.request(method, url, headers=headers, json={} if method != "GET" else None)
                    self.assertIn(response.status_code, (401, 404))
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.client.cookies.clear()
                    self.client.cookies.update(cookies)
        self.assertEqual(self.lab.response(self.job_id)["action"]["status"], "pending")

    def test_browser_csrf_and_read_auth_and_capability(self):
        with patch.object(api, "_service", return_value=self.service):
            self.assertEqual(self.client.get("/api/profile").json()["capabilities"], {"nutrition_lab": True})
            self.assertEqual(self.client.get(ROOT).status_code, 200)
            self.assertEqual(self.client.put(ROOT + "/" + self.job_id, json={}).status_code, 403)
            self.assertEqual(self.client.post(ROOT + "/" + self.job_id + "/confirm", headers={"Origin": "https://evil.example"}, json={}).status_code, 403)
            self.client.cookies.clear()
            self.assertEqual(self.client.get(ROOT).status_code, 401)

    def test_http_real_bytes_roundtrip_and_client_partition_is_rejected(self):
        with patch.object(api, "_service", return_value=self.service), patch.object(api, "_nutrition_lab", return_value=self.lab):
            payload = {"image_base64": base64.b64encode(_jpg_bytes()).decode(), "caption": "", "mode": "estimate"}
            response = self.client.put(ROOT + "/" + self.job_id, json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(self.s3.put_object.call_args.kwargs["Body"], _jpg_bytes())
            payload["user_id"] = "someone-else"
            self.assertEqual(self.client.put(ROOT + "/" + "b" * 32, json=payload, headers=self.headers).status_code, 400)
            self.assertEqual(self.client.get(ROOT + "/../other").status_code, 404)

    def test_crashed_job_has_bounded_polling_and_reset_cannot_recreate_job(self):
        self.lab.submit(self.job_id, _jpg_bytes(), "", "log")
        self.now += timedelta(seconds=181)
        self.assertEqual(self.lab.response(self.job_id)["status"], "failed")
        self.table.items.pop((USER_PK, "LAB_JOB#" + self.job_id))
        with self.assertRaises(KeyError):
            asyncio.run(self.lab.process(self.job_id, "analyze", estimator=self.estimator))
        self.assertEqual(len(self.responses.calls), 0)


if __name__ == "__main__":
    unittest.main()
