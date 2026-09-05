"""Dev-only browser adapter orchestration. Nutrition logic lives in the service."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from typing import Any

from PIL import Image, UnidentifiedImageError
from boto3.dynamodb.conditions import Key

from .serverless_auth import web_credential_key
from .serverless_data import _from_storage, _to_storage, _is_conditional_failure, epoch_seconds

USERNAME = "javaan-e2e"
USER_ID = "e2e-javaan-e2e"
IDENTITY_PK = "IDENTITY#E2E#javaan-e2e"
USER_PK = f"USER#{USER_ID}"
MAX_IMAGE_BYTES = 3_000_000  # JSON/base64 fits the API Lambda invocation limit.
MAX_BODY_BYTES = 4_010_000
JOB_TTL_SECONDS = 86400


class LabUnavailable(PermissionError):
    pass


class LabConflict(ValueError):
    pass


def enabled() -> bool:
    return (
        os.getenv("ENVIRONMENT") == "dev"
        and os.getenv("STACK_NAME") == "tg-macros-dev"
        and os.getenv("AWS_REGION") == "ap-southeast-1"
        and os.getenv("E2E_NUTRITION_LAB_ENABLED") == "true"
    )


def require_identity(repository: Any, identity: Any = None):
    """Fail closed on deployment and every canonical synthetic-account record."""
    if not enabled():
        raise LabUnavailable("Nutrition Lab unavailable.")
    expected = {"username": USERNAME, "user_id": USER_ID, "telegram_user_id": 0}
    records = [
        ({"PK": "E2E_ACCOUNT#javaan-e2e", "SK": "META"},
         {**expected, "entity_type": "e2e_account_marker", "account_type": "e2e", "identity_pk": IDENTITY_PK}),
        ({"PK": IDENTITY_PK, "SK": "USER"},
         {**expected, "entity_type": "identity", "account_type": "e2e"}),
        ({"PK": web_credential_key(USERNAME), "SK": "META"},
         {**expected, "entity_type": "web_credential", "identity_pk": IDENTITY_PK}),
    ]
    for key, fields in records:
        item = repository._get(key)
        if not item or any(item.get(k) != v for k, v in {**key, **fields}.items()):
            raise LabUnavailable("Nutrition Lab unavailable.")
    canonical = repository.get_identity_by_key(IDENTITY_PK)
    active = identity or canonical
    if (active is None or active.user_id != USER_ID or active.identity_pk != IDENTITY_PK
            or active.telegram_user_id != 0 or active.username != USERNAME):
        raise LabUnavailable("Nutrition Lab unavailable.")
    return active


def validate_job_id(job_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("Invalid Lab job id.")
    return job_id


def validate_image(image: bytes) -> None:
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise ValueError("Choose an image of at most 3 MB.")
    try:
        with Image.open(io.BytesIO(image)) as opened:
            if opened.format not in {"JPEG", "PNG", "WEBP"} or opened.width * opened.height > 25_000_000:
                raise ValueError("Choose a JPEG, PNG or WebP image of at most 25 megapixels.")
            opened.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as err:
        raise ValueError("The file is not a valid meal image.") from err


class NutritionLab:
    def __init__(self, service, *, s3=None, lambda_client=None):
        self.service = service
        self.repo = service.repository
        self.identity = require_identity(self.repo)
        self.s3 = s3
        self.lambda_client = lambda_client

    def _clients(self):
        import boto3
        if self.s3 is None:
            self.s3 = boto3.client("s3")
        if self.lambda_client is None:
            self.lambda_client = boto3.client("lambda")

    def _key(self, job_id):
        return {"PK": USER_PK, "SK": f"LAB_JOB#{validate_job_id(job_id)}"}

    def read(self, job_id):
        item = self.repo._get(self._key(job_id))
        if not item or item.get("expires_at", 0) <= epoch_seconds(self.repo._now()):
            raise KeyError("Lab job not found.")
        return _from_storage(item)

    def _put(self, item, expected=None):
        kwargs = {"Item": _to_storage(item)}
        if expected is None:
            kwargs["ConditionExpression"] = "attribute_not_exists(PK)"
        else:
            kwargs.update(ConditionExpression="#status = :expected", ExpressionAttributeNames={"#status": "status"},
                          ExpressionAttributeValues={":expected": expected})
        self.repo.table.put_item(**kwargs)

    def _dispatch(self, job_id, operation):
        self._clients()
        response = self.lambda_client.invoke(
            FunctionName=os.environ["E2E_NUTRITION_LAB_FUNCTION"], InvocationType="Event",
            Payload=json.dumps({"job_id": job_id, "operation": operation}).encode(),
        )
        if response.get("StatusCode") != 202:
            raise RuntimeError("Lab dispatch failed")

    def submit(self, job_id, image, caption, mode):
        validate_job_id(job_id)
        if mode not in {"estimate", "log"} or not isinstance(caption, str) or len(caption) > 1000:
            raise ValueError("Choose estimate or log mode and a caption of at most 1000 characters.")
        validate_image(image)
        digest = hashlib.sha256(image + json.dumps([caption, mode]).encode()).hexdigest()
        try:
            previous = self.read(job_id)
        except KeyError:
            previous = None
        if previous:
            if previous["request_hash"] != digest:
                raise LabConflict("That request id was already used for another image or mode.")
            if previous["status"] == "queued":
                self._dispatch(job_id, "analyze")
            return self.response(job_id)
        if self.repo._get(self._key(job_id)):
            raise LabConflict("That Lab request has expired. Submit with a new request id.")
        now = epoch_seconds(self.repo._now())
        object_key = f"nutrition-lab/{job_id}/{digest}"
        self._clients()
        self.s3.put_object(Bucket=os.environ["E2E_NUTRITION_LAB_BUCKET"], Key=object_key, Body=image,
                           ContentType="application/octet-stream", ServerSideEncryption="AES256")
        item = {**self._key(job_id), "entity_type": "nutrition_lab_job", "job_id": job_id,
                "status": "queued", "mode": mode, "caption": caption, "image_key": object_key,
                "request_hash": digest, "created_at": now, "expires_at": now + JOB_TTL_SECONDS}
        try:
            self._put(item)
        except Exception as err:
            if _is_conditional_failure(err):
                return self.submit(job_id, image, caption, mode)
            raise
        self._dispatch(job_id, "analyze")
        return self.response(job_id)

    @staticmethod
    def update_id(job_id):
        return -int(job_id[:22], 16)  # Synthetic partition only; stable durable retry lookup.

    def _action(self, item):
        return self.service.find_action_for_update(self.identity, self.update_id(item["job_id"])) if item["mode"] == "log" else None

    def response(self, job_id):
        item = self.read(job_id)
        result = {key: item[key] for key in ("job_id", "mode", "status", "created_at", "caption", "estimate", "model", "usage", "error", "recommendation", "recommendation_status") if key in item}
        action = self._action(item)
        if action:
            from .formatting import build_adjustment_keyboard, format_pending_message
            result["action"] = {"token": action.token, "meal_id": action.meal_id, "status": action.status,
                                "expires_at": action.expires_at, "estimate": action.estimate.to_payload(),
                                "original_estimate": action.original_estimate.to_payload(),
                                "message": format_pending_message(action)}
            result["corrections"] = [
                {"label": button.text, "type": button.callback_data.split(":")[3], "value": button.callback_data.split(":")[4]}
                for row in build_adjustment_keyboard(action).inline_keyboard for button in row
                if ":fix:" in (button.callback_data or "")
            ]
        if item["status"] in {"queued", "running"} and epoch_seconds(self.repo._now()) - item["created_at"] > 180:
            result["status"] = "failed"
            result["error"] = "Analysis timed out. Any durable meal is shown below; submit a new image to retry."
        if item.get("recommendation_status") in {"queued", "running"} and epoch_seconds(self.repo._now()) - item.get("recommendation_started_at", 0) > 180:
            result["recommendation_status"] = "failed"
        return result

    def recent(self):
        items = self.repo._query(Key("PK").eq(USER_PK) & Key("SK").begins_with("LAB_JOB#"))
        live = sorted((item for item in items if item.get("expires_at", 0) > epoch_seconds(self.repo._now())),
                      key=lambda item: item["created_at"], reverse=True)[:10]
        return [self.response(item["job_id"]) for item in live]

    def mutate(self, job_id, operation, payload):
        item = self.read(job_id)
        if item["status"] in {"queued", "running"} and epoch_seconds(self.repo._now()) - item["created_at"] <= 180:
            raise LabConflict("Analysis is still finishing. Wait for the result.")
        action = self._action(item)
        if action is None:
            raise KeyError("No durable meal exists for this Lab job.")
        if operation == "correct":
            self.service.apply_correction(self.identity, action.token, payload.get("type", ""), payload.get("value", ""))
        elif operation in {"confirm", "cancel"}:
            result = self.service.finalize_action(self.identity, action.token, operation)
            if result.status == "confirmed" and self.service.should_recommend_after_meal(self.identity, result.meal.eaten_at):
                # Persist confirmation before the independent recommendation job.
                # Retries may redispatch; the worker's conditional claim deduplicates.
                if not item.get("recommendation_status"):
                    self.repo.table.update_item(
                        Key=self._key(job_id),
                        UpdateExpression="SET recommendation_status = :queued, recommendation_started_at = :now",
                        ExpressionAttributeValues={":queued": "queued", ":now": epoch_seconds(self.repo._now())},
                        ConditionExpression="attribute_not_exists(recommendation_status)",
                    )
                if self.read(job_id).get("recommendation_status") == "queued":
                    self._dispatch(job_id, "recommend")
        else:
            raise ValueError("Unsupported Lab action.")
        return self.response(job_id)

    async def process(self, job_id, operation, *, estimator=None):
        item = self.read(job_id)
        if operation == "recommend":
            return await self._recommend(item)
        if operation != "analyze" or item["status"] != "queued":
            return
        try:
            self._put({**item, "status": "running"}, expected="queued")
        except Exception as err:
            if _is_conditional_failure(err):
                return
            raise
        self._clients()
        try:
            image = self.s3.get_object(Bucket=os.environ["E2E_NUTRITION_LAB_BUCKET"], Key=item["image_key"])["Body"].read(MAX_IMAGE_BYTES + 1)
            validate_image(image)
            estimation = await self.service.analyze_meal_image(self.identity, image, item["caption"], estimator=estimator)
            if item["mode"] == "log":
                self.service.create_pending_meal(
                    self.identity, chat_id=0, request_message_id=0, caption=item["caption"],
                    estimate=estimation.estimate, eaten_at=self.service.current_local_now_utc(self.identity),
                    username=USERNAME, update_id=self.update_id(job_id),
                    model_metadata={"model": estimation.model, "usage": estimation.usage, "source": "e2e_nutrition_lab", "job_id": job_id},
                )
            item.update(status="complete", estimate=estimation.estimate.to_payload(), model=estimation.model, usage=estimation.usage)
        except Exception as err:
            # Do not store SDK errors: they may contain secrets, captions or image data.
            import logging
            logging.getLogger(__name__).warning("lab_analysis_failed error_category=%s", type(err).__name__)
            item.update(status="failed", error="Analysis failed. Check the image and try a new run.")
        finally:
            try:
                self.s3.delete_object(Bucket=os.environ["E2E_NUTRITION_LAB_BUCKET"], Key=item["image_key"])
            finally:
                self._put(item, expected="running")

    async def _recommend(self, item):
        action = self._action(item)
        if action is None or action.status != "confirmed" or item.get("recommendation_status") != "queued":
            return
        try:
            self.repo.table.update_item(
                Key=self._key(item["job_id"]),
                UpdateExpression="SET recommendation_status = :running",
                ConditionExpression="recommendation_status = :queued",
                ExpressionAttributeValues={":running": "running", ":queued": "queued"},
            )
        except Exception as err:
            if _is_conditional_failure(err):
                return
            raise
        try:
            result, _prepared = await self.service.recommendation_async(self.identity)
            payload, status = result.to_payload(), "complete"
        except Exception:
            payload, status = {"error": "Recommendation unavailable; the meal remains confirmed."}, "failed"
        self.repo.table.update_item(
            Key=self._key(item["job_id"]),
            UpdateExpression="SET recommendation_status = :status, recommendation = :result",
            ExpressionAttributeValues=_to_storage({":status": status, ":result": payload, ":running": "running"}),
            ConditionExpression="recommendation_status = :running",
        )
