import copy
import asyncio
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

from lambda_handlers import api, webhook, worker


class ConditionalFailure(Exception):
    pass


class FakeDynamoExceptions:
    ConditionalCheckFailedException = ConditionalFailure


class FakeDynamo:
    exceptions = FakeDynamoExceptions

    def __init__(self):
        self.items = {}
        self.put_calls = []
        self._lock = threading.RLock()

    def put_item(self, **kwargs):
        with self._lock:
            self.put_calls.append(kwargs)
            key = (kwargs["Item"]["PK"]["S"], kwargs["Item"]["SK"]["S"])
            if key in self.items:
                raise ConditionalFailure()
            self.items[key] = copy.deepcopy(kwargs["Item"])

    def get_item(self, **kwargs):
        key = (kwargs["Key"]["PK"]["S"], kwargs["Key"]["SK"]["S"])
        item = self.items.get(key)
        return {"Item": copy.deepcopy(item)} if item else {}

    @staticmethod
    def _plain(value):
        if isinstance(value, dict):
            if "N" in value:
                return int(value["N"])
            if "S" in value:
                return value["S"]
        return value

    def _condition_matches(self, expression, item, values, names):
        if not expression:
            return True
        if item is None:
            return False
        status_name = names.get("#status", "status")
        if "#status = :processing" in expression and self._plain(item.get(status_name)) != self._plain(values[":processing"]):
            return False
        if "claim_token = :claim_token" in expression and self._plain(item.get("claim_token")) != self._plain(values[":claim_token"]):
            return False
        if "attribute_not_exists(lease_expires_at)" in expression:
            lease_ok = "lease_expires_at" not in item or self._plain(item["lease_expires_at"]) <= self._plain(values[":now"])
            if not lease_ok:
                return False
        return True

    def update_item(self, **kwargs):
        with self._lock:
            key = (kwargs["Key"]["PK"]["S"], kwargs["Key"]["SK"]["S"])
            item = self.items.get(key)
            values = kwargs.get("ExpressionAttributeValues", {})
            names = kwargs.get("ExpressionAttributeNames", {})
            if not self._condition_matches(kwargs.get("ConditionExpression"), item, values, names):
                raise ConditionalFailure()
            item = copy.deepcopy(item or {})
            expression = kwargs.get("UpdateExpression", "")
            set_part = expression.split("SET ", 1)[1] if "SET " in expression else ""
            for marker in (" REMOVE ", " ADD "):
                if marker in set_part:
                    set_part = set_part.split(marker, 1)[0]
            for assignment in [part.strip() for part in set_part.split(",") if part.strip()]:
                name, value_name = [part.strip() for part in assignment.split("=", 1)]
                item[names.get(name, name)] = copy.deepcopy(values[value_name])
            if "REMOVE " in expression:
                remove_part = expression.split("REMOVE ", 1)[1]
                if " ADD " in remove_part:
                    remove_part = remove_part.split(" ADD ", 1)[0]
                for name in remove_part.split(","):
                    item.pop(names.get(name.strip(), name.strip()), None)
            if "ADD " in expression:
                add_part = expression.split("ADD ", 1)[1]
                for assignment in add_part.split(","):
                    name, value_name = assignment.strip().split()
                    current = self._plain(item.get(names.get(name, name), {"N": "0"}))
                    increment = self._plain(values[value_name])
                    item[names.get(name, name)] = {"N": str(current + increment)}
            self.items[key] = item

    def delete_item(self, **kwargs):
        with self._lock:
            key = (kwargs["Key"]["PK"]["S"], kwargs["Key"]["SK"]["S"])
            self.items.pop(key, None)


class ServerlessFoundationTests(unittest.TestCase):
    def test_idempotency_claim_is_conditional_and_duplicate_is_rejected(self):
        client = FakeDynamo()
        self.assertTrue(worker.claim_idempotency(client, "idempotency", 1001, 7))
        self.assertFalse(worker.claim_idempotency(client, "idempotency", 1001, 7))
        worker.complete_idempotency(client, "idempotency", 1001)
        self.assertEqual(client.items[("TELEGRAM_UPDATE#foundation#1001", "RECORD")]["status"], {"S": "completed"})
        self.assertEqual(client.put_calls[0]["ConditionExpression"], "attribute_not_exists(PK)")

    def test_processing_lease_is_not_stolen_until_expired(self):
        client = FakeDynamo()
        first = worker.claim_idempotency_result(client, "idempotency", 1002, 7, lease_seconds=240)
        second = worker.claim_idempotency_result(client, "idempotency", 1002, 7, lease_seconds=240)
        self.assertTrue(first.claimed)
        self.assertEqual(first.attempt_count, 1)
        self.assertFalse(second.claimed)
        self.assertEqual(second.reason, "lease_active")

    def test_expired_processing_lease_is_reclaimed_atomically(self):
        client = FakeDynamo()
        first = worker.claim_idempotency_result(client, "idempotency", 1003, 7, lease_seconds=240)
        key = ("TELEGRAM_UPDATE#foundation#1003", "RECORD")
        client.items[key]["lease_expires_at"] = {"N": "0"}
        second = worker.claim_idempotency_result(client, "idempotency", 1003, 7, lease_seconds=240)
        self.assertTrue(first.claimed)
        self.assertTrue(second.claimed)
        self.assertEqual(second.reason, "reclaimed")
        self.assertEqual(client.items[key]["attempt_count"], {"N": "2"})
        self.assertNotEqual(first.claim_token, second.claim_token)

    def test_completed_record_cannot_be_reclaimed(self):
        client = FakeDynamo()
        claim = worker.claim_idempotency_result(client, "idempotency", 1004, 7, lease_seconds=0)
        worker.complete_idempotency(client, "idempotency", 1004, claim_token=claim.claim_token)
        result = worker.claim_idempotency_result(client, "idempotency", 1004, 7, lease_seconds=0)
        self.assertFalse(result.claimed)
        self.assertEqual(result.reason, "completed")

    def test_two_concurrent_reclaims_have_one_owner(self):
        client = FakeDynamo()
        worker.claim_idempotency_result(client, "idempotency", 1005, 7, lease_seconds=0)

        def reclaim():
            return worker.claim_idempotency_result(client, "idempotency", 1005, 7, lease_seconds=240)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: reclaim(), range(2)))
        self.assertEqual(sum(result.claimed for result in results), 1)
        self.assertEqual(client.items[("TELEGRAM_UPDATE#foundation#1005", "RECORD")]["attempt_count"], {"N": "2"})

    def test_retryable_failure_releases_lease_and_eventual_retry_completes_once(self):
        client = FakeDynamo()
        event = {
            "Records": [
                {
                    "messageId": "retryable-1",
                    "body": json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "telegram_update",
                            "update_id": 1006,
                            "telegram_user_id": 7,
                            "payload": {},
                        }
                    ),
                }
            ]
        }
        process = AsyncMock(side_effect=[TimeoutError(), None])
        with patch("lambda_handlers.worker._dynamodb", return_value=client), patch(
            "lambda_handlers.worker.process_update_message", new=process
        ), patch.dict(os.environ, {"IDEMPOTENCY_TABLE": "idempotency"}, clear=False):
            first_result = worker.handler(event, None)
            second_result = worker.handler(event, None)
            third_result = worker.handler(event, None)
        item = client.items[("TELEGRAM_UPDATE#telegram#1006", "RECORD")]
        self.assertEqual(first_result, {"batchItemFailures": [{"itemIdentifier": "retryable-1"}]})
        self.assertEqual(second_result, {"batchItemFailures": []})
        self.assertEqual(third_result, {"batchItemFailures": []})
        self.assertEqual(process.await_count, 2)
        self.assertEqual(item["status"], {"S": "completed"})
        self.assertEqual(item["attempt_count"], {"N": "2"})
        self.assertNotIn("lease_expires_at", item)

    def test_timeout_like_crash_without_release_is_reclaimable_after_lease_expiry(self):
        client = FakeDynamo()
        event = {
            "Records": [
                {
                    "messageId": "crash-retry-1",
                    "body": json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "telegram_update",
                            "update_id": 1008,
                            "telegram_user_id": 7,
                            "payload": {},
                        }
                    ),
                }
            ]
        }
        first = worker.claim_idempotency_result(client, "idempotency", 1008, 7, source="telegram", lease_seconds=240)
        key = ("TELEGRAM_UPDATE#telegram#1008", "RECORD")
        # A Lambda timeout can terminate execution before release_idempotency;
        # the next delivery must recover solely from the expired lease.
        client.items[key]["lease_expires_at"] = {"N": "0"}
        process = AsyncMock()
        with patch("lambda_handlers.worker._dynamodb", return_value=client), patch(
            "lambda_handlers.worker.process_update_message", new=process
        ), patch.dict(os.environ, {"IDEMPOTENCY_TABLE": "idempotency"}, clear=False):
            result = worker.handler(event, None)
        item = client.items[key]
        self.assertTrue(first.claimed)
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(process.await_count, 1)
        self.assertEqual(item["status"], {"S": "completed"})
        self.assertEqual(item["attempt_count"], {"N": "2"})

    def test_non_retryable_failure_is_terminal_and_not_retried(self):
        client = FakeDynamo()
        event = {
            "Records": [
                {
                    "messageId": "invalid-terminal-1",
                    "body": json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "telegram_update",
                            "update_id": 1007,
                            "telegram_user_id": 7,
                            "payload": {},
                        }
                    ),
                }
            ]
        }
        process = AsyncMock(side_effect=worker.NonRetryableUpdate("bad input"))
        with patch("lambda_handlers.worker._dynamodb", return_value=client), patch(
            "lambda_handlers.worker.process_update_message", new=process
        ), patch.dict(os.environ, {"IDEMPOTENCY_TABLE": "idempotency"}, clear=False):
            result = worker.handler(event, None)
        item = client.items[("TELEGRAM_UPDATE#telegram#1007", "RECORD")]
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(item["status"], {"S": "completed"})
        self.assertEqual(item["completion_reason"], {"S": "non_retryable"})

    def test_worker_validates_message_and_acknowledges_success(self):
        client = FakeDynamo()
        event = {
            "Records": [
                {
                    "messageId": "message-1",
                    "body": json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "telegram_update",
                            "update_id": 1,
                            "telegram_user_id": 7,
                            "payload": {},
                        }
                    ),
                }
            ]
        }
        with patch("lambda_handlers.worker._dynamodb", return_value=client), patch.dict(
            os.environ, {"IDEMPOTENCY_TABLE": "idempotency"}, clear=False
        ):
            result = worker.handler(event, None)
        self.assertEqual(result, {"batchItemFailures": []})

    def test_duplicate_telegram_update_is_acknowledged_without_second_processing_claim(self):
        client = FakeDynamo()
        body = json.dumps(
            {
                "schema_version": 1,
                "kind": "telegram_update",
                "update_id": 77,
                "telegram_user_id": 7,
                "payload": {},
            }
        )
        event = {"Records": [{"messageId": "one", "body": body}, {"messageId": "two", "body": body}]}
        with patch("lambda_handlers.worker._dynamodb", return_value=client), patch.dict(
            os.environ, {"IDEMPOTENCY_TABLE": "idempotency"}, clear=False
        ):
            result = worker.handler(event, None)
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(len(client.items), 1)

    def test_worker_returns_partial_failure_for_retry(self):
        client = FakeDynamo()
        event = {
            "Records": [
                {
                    "messageId": "poison-1",
                    "body": json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "telegram_update",
                            "update_id": 2,
                            "payload": {"foundation_test_behavior": "fail"},
                        }
                    ),
                }
            ]
        }
        with patch("lambda_handlers.worker._dynamodb", return_value=client), patch.dict(
            os.environ, {"IDEMPOTENCY_TABLE": "idempotency"}, clear=False
        ):
            result = worker.handler(event, None)
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "poison-1"}]})

    def test_worker_acknowledges_non_retryable_application_input(self):
        client = FakeDynamo()
        event = {
            "Records": [
                {
                    "messageId": "invalid-input",
                    "body": json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "telegram_update",
                            "update_id": 88,
                            "payload": {"message": {"text": "invalid"}},
                        }
                    ),
                }
            ]
        }
        with patch("lambda_handlers.worker._dynamodb", return_value=client), patch(
            "lambda_handlers.worker.process_update_message",
            new=AsyncMock(side_effect=worker.NonRetryableUpdate("bad input")),
        ), patch.dict(os.environ, {"IDEMPOTENCY_TABLE": "idempotency"}, clear=False):
            result = worker.handler(event, None)
        self.assertEqual(result, {"batchItemFailures": []})

    def test_webhook_rejects_missing_secret(self):
        event = {"headers": {}, "body": "{}"}
        with patch.dict(os.environ, {"WEBHOOK_SECRET_PARAMETER_NAME": ""}, clear=False):
            webhook._secret_cache = None
            result = webhook.handler(event, None)
        self.assertEqual(result["statusCode"], 403)

    def test_api_health_reports_header_presence_without_returning_header(self):
        asyncio.set_event_loop(asyncio.new_event_loop())
        event = {
            "version": "2.0",
            "routeKey": "GET /api/health",
            "rawPath": "/api/health",
            "rawQueryString": "",
            "headers": {"x-telegram-init-data": "synthetic"},
            "requestContext": {
                "http": {"method": "GET", "path": "/api/health", "sourceIp": "127.0.0.1"},
                "stage": "$default",
            },
            "isBase64Encoded": False,
        }
        result = api.handler(event, {})
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertTrue(body["telegram_init_data_present"])
        self.assertNotIn("synthetic", result["body"])
        self.assertEqual(result["headers"]["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
