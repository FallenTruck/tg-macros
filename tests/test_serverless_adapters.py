import asyncio
import io
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from lambda_handlers import api
from lambda_handlers.worker import process_update_message
from macro_bot.direct_estimator import EstimationResult
from macro_bot.models import PendingMealAction
from macro_bot.serverless_data import FinalizeResult, ServerlessIdentity
from tests.test_serverless_auth import _init_data
from tests.test_serverless_data import _estimate


class _FakeApiService:
    def __init__(self):
        self.identity = ServerlessIdentity(101, "internal-a", "a", "A", "now", "now")
        self.saved_payload = None

    def resolve_user(self, telegram_user_id, username, display_name):
        self.identity = ServerlessIdentity(telegram_user_id, "internal-a", username, display_name, "now", "now")
        return self.identity

    def profile_response(self, identity):
        return {"profile": None, "viewer": {"telegram_user_id": identity.telegram_user_id}}

    def save_profile(self, identity, payload):
        self.saved_payload = payload
        return {"profile": None, "viewer": {"telegram_user_id": identity.telegram_user_id}}


class _FakeFile:
    async def download_to_memory(self, out):
        out.write(self.data)

    def __init__(self, data):
        self.data = data


class _FakeBot:
    def __init__(self, image_bytes):
        self.image_bytes = image_bytes
        self.sent = []
        self.edited = []
        self.answers = []
        self.next_message_id = 500

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        result = SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        return result

    async def get_file(self, file_id):
        return _FakeFile(self.image_bytes)

    async def answer_callback_query(self, **kwargs):
        self.answers.append(kwargs)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)


class _FakeWorkerService:
    def __init__(self):
        self.identity = ServerlessIdentity(101, "internal-a", "a", "A", "now", "now")
        self.workflow = None
        self.action = None
        self.calls = []

        class Repo:
            def __init__(self, owner):
                self.owner = owner

            def get_workflow(self, user_id):
                return self.owner.workflow

        self.repository = Repo(self)

    def resolve_user(self, telegram_user_id, username, display_name):
        self.calls.append(("resolve", telegram_user_id))
        return self.identity

    def begin_logmeal(self, identity):
        self.workflow = {"state": "awaiting_datetime"}
        self.calls.append(("begin", identity.user_id))

    def normalize_user_datetime(self, identity, value):
        return "2026-01-15T04:00:00Z"

    def set_meal_datetime(self, identity, value):
        self.workflow = {"state": "datetime_selected", "pending_datetime": value}
        return True

    def consume_meal_datetime(self, identity):
        return None

    def peek_meal_datetime(self, identity):
        return None

    def current_local_now_utc(self, identity):
        return datetime(2026, 1, 15, 4, tzinfo=timezone.utc)

    def persona_hint(self, identity, caption):
        return ""

    def find_action_for_update(self, identity, update_id):
        return None

    def create_pending_meal(self, identity, **kwargs):
        self.action = PendingMealAction(
            token="secure-token",
            chat_id=kwargs["chat_id"],
            request_message_id=kwargs["request_message_id"],
            telegram_user_id=identity.telegram_user_id,
            username=identity.username,
            caption=kwargs["caption"],
            estimate=kwargs["estimate"],
            meal_id="meal-a",
            canonical_sk="MEAL#x#meal-a",
            expires_at=2_000_000_000,
            original_estimate=kwargs["estimate"],
        )
        return self.action

    def set_action_message_id(self, identity, token, message_id):
        self.action.message_id = message_id

    def get_action(self, identity, token):
        return self.action if self.action and token == self.action.token else None

    def scale_action(self, identity, token, factor):
        self.action.scale(factor)
        return self.action

    def finalize_action(self, identity, token, operation):
        self.action.status = "confirmed" if operation == "confirm" else "cancelled"
        return FinalizeResult(self.action.status, self.action, None, duplicate=False)

    def recommendation(self, identity):
        raise KeyError("profile missing")


class _FakeEstimator:
    async def estimate(self, image_bytes, caption="", persona_hint=""):
        return EstimationResult(_estimate(), "test-model", {})


def _jpg_bytes():
    output = io.BytesIO()
    Image.new("RGB", (10, 10), color="white").save(output, format="JPEG")
    return output.getvalue()


class ServerlessAdapterTests(unittest.TestCase):
    def test_api_uses_verified_telegram_identity_and_rejects_invalid_init_data(self):
        service = _FakeApiService()
        client = TestClient(api.app)
        valid = _init_data()
        with patch.object(api, "_service", return_value=service), patch.object(
            api, "bot_token_from_environment", return_value="test-token"
        ):
            response = client.get("/api/profile", headers={"X-Telegram-Init-Data": valid})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["viewer"]["telegram_user_id"], 101)
            response = client.get("/api/profile", headers={"X-Telegram-Init-Data": "bad"})
            self.assertEqual(response.status_code, 401)

    def test_worker_persists_workflow_boundary_and_direct_photo_flow_uses_no_http_client(self):
        service = _FakeWorkerService()
        bot = _FakeBot(_jpg_bytes())
        estimator = _FakeEstimator()
        asyncio.run(
            process_update_message(
                {
                    "update_id": 1,
                    "payload": {"message": {"message_id": 10, "chat": {"id": 99}, "from": {"id": 101}, "text": "/logmeal"}},
                },
                service=service,
                bot=bot,
                estimator=estimator,
            )
        )
        self.assertEqual(service.workflow["state"], "awaiting_datetime")
        asyncio.run(
            process_update_message(
                {
                    "update_id": 2,
                    "payload": {"message": {"message_id": 11, "chat": {"id": 99}, "from": {"id": 101}, "text": "15-01-2026 12:00"}},
                },
                service=service,
                bot=bot,
                estimator=estimator,
            )
        )
        self.assertEqual(service.workflow["state"], "datetime_selected")
        service.workflow = None
        asyncio.run(
            process_update_message(
                {
                    "update_id": 3,
                    "payload": {"message": {"message_id": 12, "chat": {"id": 99}, "from": {"id": 101}, "photo": [{"file_id": "photo-1"}], "caption": "rice bowl"}},
                },
                service=service,
                bot=bot,
                estimator=estimator,
            )
        )
        self.assertIsNotNone(service.action)
        self.assertIn("Macro estimate", bot.sent[-1]["text"])
        self.assertEqual(bot.sent[-1]["reply_markup"].inline_keyboard[0][0].callback_data, "meal:v1:confirm:secure-token")

    def test_openapp_uses_deployed_mini_app_url(self):
        service = _FakeWorkerService()
        bot = _FakeBot(_jpg_bytes())
        with patch.dict("os.environ", {"MINI_APP_URL": "https://d1example.cloudfront.net"}, clear=False):
            asyncio.run(
                process_update_message(
                    {
                        "update_id": 4,
                        "payload": {"message": {"message_id": 13, "chat": {"id": 99}, "from": {"id": 101}, "text": "/openapp"}},
                    },
                    service=service,
                    bot=bot,
                    estimator=_FakeEstimator(),
                )
            )
        self.assertEqual(
            bot.sent[-1]["reply_markup"].inline_keyboard[0][0].url,
            "https://d1example.cloudfront.net",
        )


if __name__ == "__main__":
    unittest.main()
