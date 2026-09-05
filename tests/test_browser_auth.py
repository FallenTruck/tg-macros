import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from lambda_handlers import api
from macro_bot.serverless_auth import (
    WEB_PASSWORD_ALGORITHM,
    WEB_PASSWORD_ITERATIONS,
    WEB_PASSWORD_VERSION,
    browser_session_token_hash,
    hash_web_password,
    verify_web_password,
)
from macro_bot.serverless_data import BROWSER_SESSION_TTL_SECONDS, DynamoNutritionRepository
from macro_bot.serverless_service import NutritionService
from tests.test_serverless_auth import _init_data
from tests.test_serverless_data import _FakeTable


class _BrowserFakeTable(_FakeTable):
    def delete_item(self, **kwargs):
        self.items.pop(self._key(kwargs["Key"]), None)


class BrowserAuthenticationTests(unittest.TestCase):
    password = "correct horse battery staple"

    def setUp(self):
        self.now = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
        self.table = _BrowserFakeTable()
        self.session_number = 0

        def next_session_token():
            self.session_number += 1
            return f"opaque-browser-token-{self.session_number}"

        self.repo = DynamoNutritionRepository(
            self.table,
            table_name="fitness",
            now_fn=lambda: self.now,
            identity_id_factory=lambda: "internal-a",
            session_token_factory=next_session_token,
        )
        self.service = NutritionService(self.repo)
        self.identity = self.repo.resolve_identity(101, "telegram_a", "User A")
        self.repo.save_web_credential(
            "KnownUser",
            identity=self.identity,
            password_record=hash_web_password(self.password),
        )
        self.client = TestClient(api.app, base_url="https://testserver")

    def _api(self):
        return patch.object(api, "_service", return_value=self.service), patch.dict(
            os.environ,
            {"MINI_APP_URL": "https://testserver"},
            clear=False,
        )

    def test_password_record_is_salted_hashed_and_versioned(self):
        credential = self.repo.get_web_credential("knownuser")
        self.assertIsNotNone(credential)
        self.assertEqual(credential["password_algorithm"], WEB_PASSWORD_ALGORITHM)
        self.assertEqual(credential["password_version"], WEB_PASSWORD_VERSION)
        self.assertEqual(credential["password_iterations"], WEB_PASSWORD_ITERATIONS)
        self.assertNotIn(self.password, str(credential))
        self.assertTrue(verify_web_password(self.password, credential))
        self.assertFalse(verify_web_password("wrong password", credential))
        self.assertNotIn("expires_at", credential)

    def test_browser_login_maps_to_existing_identity_and_sets_secure_cookie(self):
        with self._api()[0], self._api()[1]:
            response = self.client.post(
                "/api/auth/login",
                json={"username": "KnownUser", "password": self.password},
                headers={"Origin": "https://testserver"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["viewer"]["telegram_user_id"], 101)
            set_cookie = response.headers["set-cookie"]
            self.assertIn("jf_session=", set_cookie)
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("Secure", set_cookie)
            self.assertIn("samesite=strict", set_cookie.lower())
            self.assertIn("Path=/", set_cookie)
            self.assertIn(f"Max-Age={BROWSER_SESSION_TTL_SECONDS}", set_cookie)

            session = self.client.get("/api/auth/session")
            self.assertEqual(session.status_code, 200)
            self.assertEqual(session.json()["authenticated"], True)
            profile = self.client.get("/api/profile")
            self.assertEqual(profile.status_code, 200)
            self.assertEqual(profile.json()["viewer"]["telegram_user_id"], 101)

            token = self.client.cookies.get(api.BROWSER_SESSION_COOKIE)
            self.assertIsNotNone(token)
            self.assertNotIn(token, self.table.items)
            self.assertIn(("WEB_SESSION#" + browser_session_token_hash(token), "META"), self.table.items)

    def test_wrong_username_and_password_share_generic_failure(self):
        with self._api()[0], self._api()[1]:
            wrong_password = self.client.post(
                "/api/auth/login",
                json={"username": "KnownUser", "password": "not the password"},
                headers={"Origin": "https://testserver"},
            )
            wrong_username = self.client.post(
                "/api/auth/login",
                json={"username": "unknown", "password": self.password},
                headers={"Origin": "https://testserver"},
            )
        self.assertEqual(wrong_password.status_code, 401)
        self.assertEqual(wrong_username.status_code, 401)
        self.assertEqual(wrong_password.json(), wrong_username.json())
        self.assertNotIn(self.password, wrong_username.text)

    def test_invalid_telegram_data_never_falls_back_to_browser_cookie(self):
        with self._api()[0], self._api()[1], patch.object(api, "bot_token_from_environment", return_value="test-token"):
            self.assertEqual(
                self.client.post(
                    "/api/auth/login",
                    json={"username": "KnownUser", "password": self.password},
                    headers={"Origin": "https://testserver"},
                ).status_code,
                200,
            )
            rejected = self.client.get("/api/profile", headers={"X-Telegram-Init-Data": "invalid-init-data"})
        self.assertEqual(rejected.status_code, 401)

    def test_valid_telegram_init_data_still_bypasses_browser_login(self):
        telegram_client = TestClient(api.app, base_url="https://testserver")
        with self._api()[0], self._api()[1], patch.object(api, "bot_token_from_environment", return_value="test-token"):
            response = telegram_client.get("/api/profile", headers={"X-Telegram-Init-Data": _init_data()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["viewer"]["telegram_user_id"], 101)

    def test_browser_session_expiry_and_logout_revoke_access(self):
        with self._api()[0], self._api()[1]:
            self.assertEqual(
                self.client.post(
                    "/api/auth/login",
                    json={"username": "KnownUser", "password": self.password},
                    headers={"Origin": "https://testserver"},
                ).status_code,
                200,
            )
            self.now = self.now + timedelta(days=31)
            self.assertFalse(self.client.get("/api/auth/session").json()["authenticated"])

            self.now = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
            token, session = self.repo.create_browser_session(self.identity)
            self.client.cookies.set(api.BROWSER_SESSION_COOKIE, token, domain="testserver", path="/")
            self.assertEqual(session["expires_at"], int(self.now.timestamp()) + BROWSER_SESSION_TTL_SECONDS)
            logout = self.client.post("/api/auth/logout", headers={"Origin": "https://testserver"})
            self.assertEqual(logout.status_code, 200)
            self.assertIn("Max-Age=0", logout.headers["set-cookie"])
            self.assertFalse(self.client.get("/api/auth/session").json()["authenticated"])

    def test_browser_csrf_guard_blocks_cross_origin_mutations_but_not_telegram(self):
        with self._api()[0], self._api()[1], patch.object(api, "bot_token_from_environment", return_value="test-token"):
            self.assertEqual(
                self.client.post(
                    "/api/auth/login",
                    json={"username": "KnownUser", "password": self.password},
                    headers={"Origin": "https://testserver"},
                ).status_code,
                200,
            )
            blocked = self.client.post(
                "/api/profile",
                json={},
                headers={"Origin": "https://attacker.example"},
            )
            self.assertEqual(blocked.status_code, 403)
            telegram = self.client.post(
                "/api/targets/preview",
                json={
                    "sex": "male",
                    "age_years": 30,
                    "height_cm": 180,
                    "weight_kg": 80,
                    "activity_level": "moderate",
                    "goal": "maintain",
                },
                headers={"X-Telegram-Init-Data": _init_data()},
            )
        self.assertEqual(telegram.status_code, 200)

    def test_two_browser_users_resolve_to_their_own_identities(self):
        identity_b = self.repo.resolve_identity(202, "telegram_b", "User B")
        self.repo.save_web_credential(
            "knownuser-b",
            identity=identity_b,
            password_record=hash_web_password(self.password),
        )
        client_b = TestClient(api.app, base_url="https://testserver")
        with self._api()[0], self._api()[1]:
            self.assertEqual(
                self.client.post(
                    "/api/auth/login",
                    json={"username": "KnownUser", "password": self.password},
                    headers={"Origin": "https://testserver"},
                ).status_code,
                200,
            )
            self.assertEqual(
                client_b.post(
                    "/api/auth/login",
                    json={"username": "knownuser-b", "password": self.password},
                    headers={"Origin": "https://testserver"},
                ).status_code,
                200,
            )
            profile_a = self.client.get("/api/profile")
            profile_b = client_b.get("/api/profile")
        self.assertEqual(profile_a.json()["viewer"]["telegram_user_id"], 101)
        self.assertEqual(profile_b.json()["viewer"]["telegram_user_id"], 202)

    def test_daily_nutrition_endpoint_has_the_same_browser_and_telegram_result(self):
        answers = {
            "sex": "male",
            "age_years": 30,
            "height_cm": 180,
            "weight_kg": 80,
            "activity_level": "moderate",
            "goal": "maintain",
        }
        self.service.save_profile(self.identity, answers)
        with self._api()[0], self._api()[1], patch.object(api, "bot_token_from_environment", return_value="test-token"):
            self.assertEqual(
                self.client.post(
                    "/api/auth/login",
                    json={"username": "KnownUser", "password": self.password},
                    headers={"Origin": "https://testserver"},
                ).status_code,
                200,
            )
            browser = self.client.get("/api/nutrition/day?date=2026-01-15")
            telegram = self.client.get("/api/nutrition/day?date=2026-01-15", headers={"X-Telegram-Init-Data": _init_data()})

        self.assertEqual(browser.status_code, 200)
        self.assertEqual(telegram.status_code, 200)
        self.assertEqual(browser.json(), telegram.json())
        self.assertEqual(browser.json()["date"], "2026-01-15")


if __name__ == "__main__":
    unittest.main()
