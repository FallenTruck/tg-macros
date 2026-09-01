import json
import asyncio
import os
import unittest
from unittest.mock import patch

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

    def put_item(self, **kwargs):
        self.put_calls.append(kwargs)
        key = (kwargs["Item"]["PK"]["S"], kwargs["Item"]["SK"]["S"])
        if key in self.items:
            raise ConditionalFailure()
        self.items[key] = kwargs["Item"]

    def update_item(self, **kwargs):
        key = (kwargs["Key"]["PK"]["S"], kwargs["Key"]["SK"]["S"])
        self.items[key]["status"] = {"S": "completed"}


class ServerlessFoundationTests(unittest.TestCase):
    def test_idempotency_claim_is_conditional_and_duplicate_is_rejected(self):
        client = FakeDynamo()
        self.assertTrue(worker.claim_idempotency(client, "idempotency", 1001, 7))
        self.assertFalse(worker.claim_idempotency(client, "idempotency", 1001, 7))
        worker.complete_idempotency(client, "idempotency", 1001)
        self.assertEqual(client.items[("TELEGRAM_UPDATE#foundation#1001", "RECORD")]["status"], {"S": "completed"})
        self.assertEqual(client.put_calls[0]["ConditionExpression"], "attribute_not_exists(PK)")

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
