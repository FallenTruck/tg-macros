import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from macro_bot.serverless_auth import TelegramAuthError, validate_init_data


def _init_data(bot_token="test-token", user_id=101, auth_date=None, init_fields=None, **user_fields):
    payload = {"id": user_id, "first_name": "Test", **user_fields}
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "synthetic-query",
        "user": json.dumps(payload, separators=(",", ":")),
    }
    fields.update(init_fields or {})
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class ServerlessAuthTests(unittest.TestCase):
    def test_valid_init_data_returns_telegram_identity(self):
        result = validate_init_data(
            _init_data(username="updated_handle", last_name="User", auth_date=1_700_000_000),
            "test-token",
            now=1_700_000_000,
            max_age_seconds=3_600,
        )
        self.assertEqual(result.telegram_user_id, 101)
        self.assertEqual(result.username, "updated_handle")
        self.assertEqual(result.display_name, "Test User")

    def test_invalid_stale_future_and_signature_data_are_rejected(self):
        with self.assertRaises(TelegramAuthError):
            validate_init_data(_init_data(auth_date=1_699_990_000), "test-token", now=1_700_000_000)
        with self.assertRaises(TelegramAuthError):
            validate_init_data(_init_data(auth_date=1_700_000_100), "test-token", now=1_700_000_000)
        with self.assertRaises(TelegramAuthError):
            validate_init_data(_init_data() + "x", "test-token", now=int(time.time()))

    def test_duplicate_query_fields_are_rejected(self):
        data = _init_data() + "&auth_date=1"
        with self.assertRaises(TelegramAuthError):
            validate_init_data(data, "test-token")


if __name__ == "__main__":
    unittest.main()
