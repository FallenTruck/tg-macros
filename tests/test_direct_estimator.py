import asyncio
import io
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from macro_bot.direct_estimator import (
    OPENAI_APPLICATION_RETRIES,
    OPENAI_REQUEST_TIMEOUT_SECONDS,
    OPENAI_SDK_RETRIES,
    MEAL_ITEM_SCHEMA,
    MEAL_MACRO_SCHEMA,
    DirectEstimationError,
    DirectOpenAIEstimator,
    validate_result,
)
from macro_bot.serverless_data import _estimate_payload
from tests.test_serverless_data import _estimate


class _Responses:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return Mock(output_text=json.dumps(self.payload), usage=None)


def _jpg_bytes():
    image = io.BytesIO()
    Image.new("RGB", (8, 8), color="white").save(image, format="JPEG")
    return image.getvalue()


def _structured_payload(*, protein=30, calories=500, item_breakdown_complete=True):
    return {
        "meal_name": "rice bowl",
        "calories": calories,
        "protein_g": protein,
        "carbs_g": 60,
        "fat_g": 10,
        "total_best": {"calories": calories, "protein_g": protein, "carbs_g": 60, "fat_g": 10},
        "total_low": {"calories": max(0, calories - 50), "protein_g": 25, "carbs_g": 50, "fat_g": 8},
        "total_high": {"calories": calories + 100, "protein_g": 38, "carbs_g": 75, "fat_g": 15},
        "items": [
            {
                "name": "rice",
                "portion_g": 250,
                "portion_low_g": 180,
                "portion_high_g": 320,
                "assumptions": "cooked rice",
                "calories": calories,
                "protein_g": protein,
                "carbs_g": 60,
                "fat_g": 10,
                "identification_confidence": 0.9,
                "portion_confidence": 0.8,
                "evidence": "clearly_visible",
            }
        ],
        "variance_drivers": ["portion size", "cooking oil"],
        "confidence": 0.8,
        "identification_confidence": 0.9,
        "portion_confidence": 0.8,
        "macro_confidence": 0.7,
        "item_breakdown_complete": item_breakdown_complete,
        "notes": "standard portion",
    }


class DirectEstimatorTests(unittest.TestCase):
    def test_estimate_uses_existing_schema_and_does_not_write_repository_metrics(self):
        responses = _Responses(_estimate_payload(_estimate()).copy())
        client = Mock(responses=responses)
        estimator = DirectOpenAIEstimator(client=client, max_retries=0)
        metrics_path = Path(__file__).resolve().parents[1] / "metrics" / "estimates.jsonl"
        before = metrics_path.read_bytes()
        image = io.BytesIO()
        Image.new("RGB", (8, 8), color="white").save(image, format="JPEG")
        result = asyncio.run(estimator.estimate(image.getvalue()))
        self.assertEqual(metrics_path.read_bytes(), before)
        self.assertEqual(result.estimate.meal_name, "rice bowl")
        self.assertEqual(result.model, "gpt-5.4")
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(responses.calls[0]["text"]["format"]["name"], "meal_macros")
        self.assertNotIn("metrics_event_id", _estimate_payload(result.estimate))

    def test_model_version_is_unconditionally_overwritten(self):
        from macro_bot.direct_estimator import ESTIMATOR_APPLICATION_VERSION
        for version in ("invented", "", None):
            payload = _structured_payload()
            payload["estimator_version"] = version
            self.assertEqual(validate_result(payload)["estimator_version"], ESTIMATOR_APPLICATION_VERSION)

    def test_openai_client_uses_bounded_timeout_and_no_sdk_retries(self):
        responses = _Responses(_estimate_payload(_estimate()).copy())
        fake_client = Mock(responses=responses)
        with patch("openai.OpenAI", return_value=fake_client) as constructor, patch(
            "macro_bot.direct_estimator.openai_key_from_environment", return_value="test-key"
        ):
            estimator = DirectOpenAIEstimator(client=None, max_retries=OPENAI_APPLICATION_RETRIES)
            asyncio.run(estimator.estimate(_jpg_bytes()))
        constructor.assert_called_once_with(
            api_key="test-key",
            timeout=OPENAI_REQUEST_TIMEOUT_SECONDS,
            max_retries=OPENAI_SDK_RETRIES,
        )

    def test_strict_schema_declares_every_quality_field_and_keeps_uncertainty_nullable(self):
        self.assertEqual(set(MEAL_ITEM_SCHEMA["properties"]), set(MEAL_ITEM_SCHEMA["required"]))
        self.assertEqual(set(MEAL_MACRO_SCHEMA["properties"]), set(MEAL_MACRO_SCHEMA["required"]))
        self.assertEqual(MEAL_ITEM_SCHEMA["properties"]["portion_low_g"]["type"], ["number", "null"])
        self.assertEqual(MEAL_MACRO_SCHEMA["properties"]["macro_confidence"]["type"], ["number", "null"])
        self.assertNotIn("follow_up_question", MEAL_MACRO_SCHEMA["properties"])

    def test_application_retry_count_is_bounded_and_telemetry_is_emitted(self):
        class FailingResponses:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                raise TimeoutError("synthetic timeout")

        responses = FailingResponses()
        events = []
        estimator = DirectOpenAIEstimator(
            client=Mock(responses=responses),
            max_retries=1,
            telemetry_callback=lambda *event: events.append(event),
        )
        with self.assertRaises(DirectEstimationError):
            asyncio.run(estimator.estimate(_jpg_bytes()))
        self.assertEqual(responses.calls, 2)
        request_events = [event for event in events if event[0] == "openai_request"]
        self.assertEqual([event[2] for event in request_events], ["started", "failure", "started", "failure"])
        self.assertEqual([event[3] for event in request_events], [1, 1, 2, 2])

    def test_success_emits_secret_request_and_validation_stage_events(self):
        responses = _Responses(_estimate_payload(_estimate()).copy())
        events = []
        estimator = DirectOpenAIEstimator(
            client=Mock(responses=responses),
            max_retries=0,
            telemetry_callback=lambda *event: events.append(event),
        )
        asyncio.run(estimator.estimate(_jpg_bytes()))
        stages = [event[0] for event in events]
        self.assertIn("openai_secret_resolution", stages)
        self.assertIn("openai_request", stages)
        self.assertIn("estimate_validation", stages)

    def test_exact_item_totals_are_reconciled_deterministically(self):
        result = validate_result(_structured_payload(calories=503))
        self.assertEqual(result["reconciliation_status"], "matched")
        self.assertEqual(result["model_reported_total"]["calories"], 503)
        self.assertEqual(result["item_derived_total"]["calories"], 503)
        self.assertEqual(result["total_best"]["calories"], 503)
        self.assertIsNone(result["follow_up_question"])

    def test_live_style_protein_mismatch_preserves_model_and_uses_valid_item_totals(self):
        payload = _structured_payload(calories=668, protein=54)
        payload["total_best"] = {"calories": 668, "protein_g": 54, "carbs_g": 61, "fat_g": 20}
        payload["total_low"] = {"calories": 600, "protein_g": 45, "carbs_g": 50, "fat_g": 15}
        payload["total_high"] = {"calories": 760, "protein_g": 70, "carbs_g": 75, "fat_g": 30}
        payload["carbs_g"] = 61
        payload["fat_g"] = 20
        payload["items"][0].update({"calories": 668, "protein_g": 61, "carbs_g": 61, "fat_g": 20})
        result = validate_result(payload)
        self.assertEqual(result["reconciliation_status"], "reconciled_from_items")
        self.assertEqual(result["model_reported_total"]["protein_g"], 54)
        self.assertEqual(result["item_derived_total"]["protein_g"], 61)
        self.assertEqual(result["total_best"]["protein_g"], 61)

    def test_incomplete_item_breakdown_keeps_model_aggregate_canonical(self):
        payload = _structured_payload(item_breakdown_complete=False)
        payload["items"][0].update({"calories": 200, "protein_g": 10, "carbs_g": 20, "fat_g": 5})
        result = validate_result(payload)
        self.assertEqual(result["reconciliation_status"], "partial_item_breakdown")
        self.assertEqual(result["model_reported_total"]["calories"], 500)
        self.assertEqual(result["item_derived_total"]["calories"], 200)
        self.assertEqual(result["total_best"]["calories"], 500)

    def test_material_aggregate_calorie_energy_mismatch_is_rejected(self):
        payload = _structured_payload()
        payload["total_best"]["calories"] = 1000
        payload["total_high"]["calories"] = 1100
        with self.assertRaises(ValueError):
            validate_result(payload)

    def test_ambiguous_high_impact_component_triggers_one_follow_up_question(self):
        payload = _structured_payload()
        payload["items"][0].update(
            {
                "name": "soba noodles",
                "evidence": "partially_occluded",
                "identification_confidence": 0.45,
                "portion_confidence": 0.3,
                "portion_low_g": 80,
                "portion_high_g": 300,
            }
        )
        result = validate_result(payload)
        self.assertIn("base under the toppings", result["follow_up_question"])

    def test_modest_uncertainty_does_not_trigger_follow_up(self):
        payload = _structured_payload()
        payload["items"][0]["evidence"] = "probably_visible"
        result = validate_result(payload)
        self.assertIsNone(result["follow_up_question"])


if __name__ == "__main__":
    unittest.main()
