import asyncio
import hashlib
import hmac
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import macro_api


class _FakeResp:
    def __init__(self, output_text):
        self.output_text = output_text


class _FakeResponses:
    def __init__(self, output_text):
        self._output_text = output_text

    def create(self, **kwargs):
        return _FakeResp(self._output_text)


class _FakeClient:
    def __init__(self, output_text):
        self.responses = _FakeResponses(output_text)


class _FakeTelegramBot:
    def __init__(self):
        self.set_webhook_calls = []

    async def set_webhook(self, **kwargs):
        self.set_webhook_calls.append(kwargs)


class _FakeTelegramApplication:
    def __init__(self):
        self.bot = _FakeTelegramBot()
        self.events = []
        self.processed_updates = []

    async def initialize(self):
        self.events.append("initialize")

    async def start(self):
        self.events.append("start")

    async def stop(self):
        self.events.append("stop")

    async def shutdown(self):
        self.events.append("shutdown")

    async def process_update(self, update):
        self.processed_updates.append(update)


class MacroApiTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.profile_path = Path(self.tmpdir.name) / "profiles.json"
        self.client = TestClient(macro_api.app)

    def tearDown(self):
        macro_api.app.state.telegram_application = None
        self.tmpdir.cleanup()

    def _jpg_bytes(self):
        buf = io.BytesIO()
        Image.new("RGB", (20, 20), color="white").save(buf, format="JPEG")
        return buf.getvalue()

    def _valid_estimate_payload(self):
        return {
            "meal_name": "meal",
            "calories": 500,
            "protein_g": 30,
            "carbs_g": 60,
            "fat_g": 10,
            "total_best": {
                "calories": 500,
                "protein_g": 30,
                "carbs_g": 60,
                "fat_g": 10,
            },
            "total_low": {
                "calories": 450,
                "protein_g": 26,
                "carbs_g": 54,
                "fat_g": 8,
            },
            "total_high": {
                "calories": 620,
                "protein_g": 35,
                "carbs_g": 75,
                "fat_g": 18,
            },
            "items": [
                {
                    "name": "fried rice",
                    "portion_g": 250,
                    "assumptions": "Assumed 1 tbsp oil used.",
                    "calories": 380,
                    "protein_g": 8,
                    "carbs_g": 62,
                    "fat_g": 11,
                },
                {
                    "name": "chicken leg",
                    "portion_g": 180,
                    "assumptions": "Assumed skin-on roasted leg.",
                    "calories": 120,
                    "protein_g": 22,
                    "carbs_g": 0,
                    "fat_g": 2,
                },
            ],
            "variance_drivers": ["oil amount", "portion size"],
            "confidence": 0.8,
            "notes": "Structured assumptions included.",
        }

    def _recommend_request(self):
        return {
            "telegram_user_id": 349553317,
            "profile": {
                "telegram_user_id": 349553317,
                "username": "Vaanasaurus",
                "display_name": "Vaan",
                "daily_target": {
                    "calories": 2200,
                    "protein_g": 160,
                    "carbs_g": 220,
                    "fat_g": 70,
                },
                "dietary_preferences": ["high protein"],
                "restrictions": [],
                "preferred_cuisines": ["asian"],
                "preferred_staples": ["rice", "chicken"],
                "preferred_tags": ["meal"],
            },
            "today_totals": {"calories": 900, "protein_g": 60, "carbs_g": 100, "fat_g": 25},
            "remaining_macros": {
                "target": {"calories": 2200, "protein_g": 160, "carbs_g": 220, "fat_g": 70},
                "consumed": {"calories": 900, "protein_g": 60, "carbs_g": 100, "fat_g": 25},
                "remaining_raw": {"calories": 1300, "protein_g": 100, "carbs_g": 120, "fat_g": 45},
                "remaining": {"calories": 1300, "protein_g": 100, "carbs_g": 120, "fat_g": 45},
                "over_calories": False,
                "over_protein": False,
                "over_carbs": False,
                "over_fat": False,
            },
            "recent_meals": ["omelette", "rice bowl"],
            "candidate_foods": [
                {
                    "food_id": "chicken_rice_bowl",
                    "name": "Chicken Rice Bowl",
                    "serving": "1 bowl",
                    "calories": 620,
                    "protein_g": 42,
                    "carbs_g": 68,
                    "fat_g": 18,
                    "tags": ["meal"],
                    "cuisines": ["asian"],
                    "fit_score": 82.0,
                    "fit_reason": "strong protein fit",
                },
                {
                    "food_id": "protein_shake",
                    "name": "Double Protein Shake",
                    "serving": "1 large shake",
                    "calories": 340,
                    "protein_g": 48,
                    "carbs_g": 18,
                    "fat_g": 7,
                    "tags": ["snack", "high_protein"],
                    "cuisines": [],
                    "fit_score": 78.0,
                    "fit_reason": "easy protein top-up",
                },
                {
                    "food_id": "egg_toast",
                    "name": "Egg Toast Plate",
                    "serving": "2 eggs + 2 toast",
                    "calories": 430,
                    "protein_g": 24,
                    "carbs_g": 34,
                    "fat_g": 18,
                    "tags": ["meal"],
                    "cuisines": ["western"],
                    "fit_score": 70.0,
                    "fit_reason": "balanced macros",
                },
            ],
        }

    def _build_init_data(self, user=None, auth_date=None, bot_token="token"):
        user = user or {
            "id": 349553317,
            "username": "Vaanasaurus",
            "first_name": "Vaan",
        }
        auth_date = auth_date or int(datetime.utcnow().timestamp())
        payload = {
            "auth_date": str(auth_date),
            "query_id": "test-query",
            "user": json.dumps(user, separators=(",", ":")),
        }
        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(payload.items())
        )
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        payload["hash"] = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return urlencode(payload)

    def test_empty_upload(self):
        response = self.client.post(
            "/estimate",
            files={"file": ("meal.jpg", b"", "image/jpeg")},
            data={"caption": "x"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Empty upload", response.text)

    def test_oversized_upload(self):
        response = self.client.post(
            "/estimate",
            files={"file": ("meal.jpg", b"x" * 6_000_001, "image/jpeg")},
            data={"caption": "x"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Image too large", response.text)

    def test_schema_compliant_estimate_success(self):
        payload = self._valid_estimate_payload()

        with patch.object(macro_api, "OPENAI_API_KEY", "key"), patch.object(
            macro_api, "client", _FakeClient(json.dumps(payload))
        ):
            response = self.client.post(
                "/estimate",
                files={"file": ("meal.jpg", self._jpg_bytes(), "image/jpeg")},
                data={"caption": "x", "persona_hint": "similar prior meal"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("items", body)
        self.assertIn("total_low", body)
        self.assertIn("total_high", body)
        self.assertEqual(body["calories"], body["total_best"]["calories"])

    def test_reject_invalid_ranges(self):
        payload = self._valid_estimate_payload()
        payload["total_low"]["calories"] = 600
        payload["total_best"]["calories"] = 500

        with patch.object(macro_api, "OPENAI_API_KEY", "key"), patch.object(
            macro_api, "client", _FakeClient(json.dumps(payload))
        ):
            response = self.client.post(
                "/estimate",
                files={"file": ("meal.jpg", self._jpg_bytes(), "image/jpeg")},
                data={"caption": "x"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("validation failed", response.text)

    def test_reject_missing_item_fields(self):
        payload = self._valid_estimate_payload()
        del payload["items"][0]["assumptions"]

        with patch.object(macro_api, "OPENAI_API_KEY", "key"), patch.object(
            macro_api, "client", _FakeClient(json.dumps(payload))
        ):
            response = self.client.post(
                "/estimate",
                files={"file": ("meal.jpg", self._jpg_bytes(), "image/jpeg")},
                data={"caption": "x"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("Missing item keys", response.text)

    def test_recommend_success(self):
        selection = {
            "summary": "You still have room for a protein-forward meal.",
            "suggestions": [
                {
                    "food_id": "chicken_rice_bowl",
                    "fit_rationale": "best overall fit",
                    "tradeoffs": "slightly heavier on carbs",
                },
                {
                    "food_id": "protein_shake",
                    "fit_rationale": "easy protein top-up",
                    "tradeoffs": "may need a fuller meal later",
                },
            ],
        }

        with patch.object(macro_api, "OPENAI_API_KEY", "key"), patch.object(
            macro_api, "client", _FakeClient(json.dumps(selection))
        ):
            response = self.client.post("/recommend", json=self._recommend_request())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "model_ranked")
        self.assertEqual(body["suggestions"][0]["name"], "Chicken Rice Bowl")
        self.assertEqual(body["suggestions"][1]["name"], "Double Protein Shake")

    def test_recommend_fallback_when_model_response_is_invalid(self):
        selection = {
            "summary": "Bad selection",
            "suggestions": [
                {
                    "food_id": "unknown_food",
                    "fit_rationale": "bad id",
                    "tradeoffs": "bad",
                }
            ],
        }

        with patch.object(macro_api, "OPENAI_API_KEY", "key"), patch.object(
            macro_api, "client", _FakeClient(json.dumps(selection))
        ):
            response = self.client.post("/recommend", json=self._recommend_request())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "deterministic_fallback")
        self.assertEqual(body["suggestions"][0]["name"], "Chicken Rice Bowl")

    def test_recommend_bad_request(self):
        payload = self._recommend_request()
        payload["candidate_foods"] = []
        response = self.client.post("/recommend", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("candidate_foods", response.text)

    def test_catalog_overlap_review_success(self):
        payload = {
            "decisions": [
                {
                    "suggestion_id": "s1",
                    "action": "reject_duplicate",
                    "duplicate_food_id": "double_protein_shake",
                    "rationale": "Same shake recipe with naming variation.",
                }
            ]
        }
        request = {
            "suggestions": [
                {
                    "suggestion_id": "s1",
                    "telegram_user_id": 349553317,
                    "proposed_name": "ON Milk Chocolate Protein Shake",
                    "proposed_serving": "1 logged portion",
                    "macros": {"calories": 397, "protein_g": 50.7, "carbs_g": 19.7, "fat_g": 5.0},
                    "source_captions": ["ON milk chocolate protein shake. 2 scoops protein and 350ml normal meiji milk"],
                }
            ],
            "catalog_entries": [
                {
                    "food_id": "double_protein_shake",
                    "name": "Double Protein Shake",
                    "serving": "2 scoops whey with milk",
                    "macros": {"calories": 380, "protein_g": 48, "carbs_g": 18, "fat_g": 6},
                    "eligible_telegram_user_ids": [349553317],
                }
            ],
        }

        with patch.object(macro_api, "OPENAI_API_KEY", "key"), patch.object(
            macro_api, "client", _FakeClient(json.dumps(payload))
        ):
            response = self.client.post("/catalog/review-overlaps", json=request)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["decisions"][0]["action"], "reject_duplicate")
        self.assertEqual(body["decisions"][0]["duplicate_food_id"], "double_protein_shake")

    def test_catalog_overlap_review_fallback_keeps_suggestions(self):
        payload = {
            "decisions": [
                {
                    "suggestion_id": "s1",
                    "action": "reject_duplicate",
                    "duplicate_food_id": "unknown_food",
                    "rationale": "bad",
                }
            ]
        }
        request = {
            "suggestions": [
                {
                    "suggestion_id": "s1",
                    "telegram_user_id": 349553317,
                    "proposed_name": "ON Milk Chocolate Protein Shake",
                    "proposed_serving": "1 logged portion",
                    "macros": {"calories": 397, "protein_g": 50.7, "carbs_g": 19.7, "fat_g": 5.0},
                    "source_captions": ["ON milk chocolate protein shake. 2 scoops protein and 350ml normal meiji milk"],
                }
            ],
            "catalog_entries": [
                {
                    "food_id": "double_protein_shake",
                    "name": "Double Protein Shake",
                    "serving": "2 scoops whey with milk",
                    "macros": {"calories": 380, "protein_g": 48, "carbs_g": 18, "fat_g": 6},
                    "eligible_telegram_user_ids": [349553317],
                }
            ],
        }

        with patch.object(macro_api, "OPENAI_API_KEY", "key"), patch.object(
            macro_api, "client", _FakeClient(json.dumps(payload))
        ):
            response = self.client.post("/catalog/review-overlaps", json=request)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["decisions"][0]["action"], "keep")

    def test_miniapp_profile_requires_valid_init_data(self):
        response = self.client.get("/miniapp/api/profile")
        self.assertEqual(response.status_code, 401)

    def test_miniapp_preview_and_save_profile(self):
        init_data = self._build_init_data()
        answers = {
            "sex": "male",
            "age_years": 31,
            "height_cm": 175,
            "weight_kg": 78,
            "activity_level": "moderate",
            "goal": "maintain",
        }

        with patch.object(macro_api, "BOT_TOKEN", "token"), patch.object(
            macro_api, "USER_PROFILES_PATH", self.profile_path
        ):
            preview_response = self.client.post(
                "/miniapp/api/targets/preview",
                json=answers,
                headers={"X-Telegram-Init-Data": init_data},
            )
            self.assertEqual(preview_response.status_code, 200)
            preview_body = preview_response.json()
            self.assertEqual(
                preview_body["activity_label"],
                "Moderately active (exercise 3-4 days/week)",
            )
            self.assertEqual(preview_body["goal_label"], "Maintain")
            self.assertGreater(preview_body["daily_target"]["calories"], 0)

            save_response = self.client.post(
                "/miniapp/api/profile",
                json=answers,
                headers={"X-Telegram-Init-Data": init_data},
            )
            self.assertEqual(save_response.status_code, 200)
            save_body = save_response.json()
            self.assertEqual(save_body["profile"]["telegram_user_id"], 349553317)
            self.assertEqual(
                save_body["profile"]["questionnaire_answers"]["activity_level"],
                "moderate",
            )
            self.assertEqual(
                save_body["profile"]["daily_target"],
                preview_body["daily_target"],
            )

            stored_payload = json.loads(self.profile_path.read_text(encoding="utf-8"))
            self.assertEqual(stored_payload["profiles"][0]["telegram_user_id"], 349553317)

            profile_response = self.client.get(
                "/miniapp/api/profile",
                headers={"X-Telegram-Init-Data": init_data},
            )
            self.assertEqual(profile_response.status_code, 200)
            profile_body = profile_response.json()
            self.assertEqual(profile_body["profile"]["display_name"], "Vaan")
            self.assertTrue(profile_body["activity_options"])

    def test_miniapp_rejects_stale_init_data(self):
        stale_auth_date = int((datetime.utcnow() - timedelta(hours=2)).timestamp())
        init_data = self._build_init_data(auth_date=stale_auth_date)

        with patch.object(macro_api, "BOT_TOKEN", "token"), patch.object(
            macro_api, "USER_PROFILES_PATH", self.profile_path
        ), patch.object(macro_api, "TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", 60):
            response = self.client.get(
                "/miniapp/api/profile",
                headers={"X-Telegram-Init-Data": init_data},
            )

        self.assertEqual(response.status_code, 401)
        self.assertIn("stale", response.text)

    def test_start_telegram_webhook_configures_bot_application(self):
        fake_application = _FakeTelegramApplication()

        with patch.object(macro_api, "BOT_TOKEN", "token"), patch.object(
            macro_api, "MINI_APP_URL", "https://javaanfitness.onrender.com/miniapp"
        ), patch.object(macro_api, "TELEGRAM_WEBHOOK_BASE_URL", ""), patch.object(
            macro_api, "TELEGRAM_WEBHOOK_SECRET", "secret-token"
        ), patch.object(
            macro_api, "build_telegram_application", return_value=fake_application
        ):
            asyncio.run(macro_api._start_telegram_webhook())

        self.assertEqual(fake_application.events, ["initialize", "start"])
        self.assertEqual(
            fake_application.bot.set_webhook_calls,
            [
                {
                    "url": "https://javaanfitness.onrender.com/telegram/webhook",
                    "secret_token": "secret-token",
                }
            ],
        )
        self.assertIs(macro_api.app.state.telegram_application, fake_application)

        asyncio.run(macro_api._stop_telegram_webhook())
        self.assertEqual(fake_application.events[-2:], ["stop", "shutdown"])

    def test_telegram_webhook_processes_update(self):
        fake_application = _FakeTelegramApplication()
        macro_api.app.state.telegram_application = fake_application

        with patch.object(macro_api.Update, "de_json", return_value={"update_id": 1}) as de_json:
            response = self.client.post(
                "/telegram/webhook",
                json={"update_id": 1, "message": {"message_id": 10}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        de_json.assert_called_once()
        self.assertEqual(fake_application.processed_updates, [{"update_id": 1}])

    def test_telegram_webhook_rejects_invalid_secret(self):
        macro_api.app.state.telegram_application = _FakeTelegramApplication()

        with patch.object(macro_api, "TELEGRAM_WEBHOOK_SECRET", "expected-secret"):
            response = self.client.post(
                "/telegram/webhook",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertIn("Invalid Telegram webhook secret", response.text)


if __name__ == "__main__":
    unittest.main()
