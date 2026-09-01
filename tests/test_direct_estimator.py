import asyncio
import io
import json
import unittest
from unittest.mock import Mock

from PIL import Image

from macro_bot.direct_estimator import DirectOpenAIEstimator
from macro_bot.serverless_data import _estimate_payload
from tests.test_serverless_data import _estimate


class _Responses:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return Mock(output_text=json.dumps(self.payload), usage=None)


class DirectEstimatorTests(unittest.TestCase):
    def test_estimate_uses_existing_schema_and_does_not_write_repository_metrics(self):
        responses = _Responses(_estimate_payload(_estimate()).copy())
        client = Mock(responses=responses)
        estimator = DirectOpenAIEstimator(client=client, max_retries=0)
        image = io.BytesIO()
        Image.new("RGB", (8, 8), color="white").save(image, format="JPEG")
        result = asyncio.run(estimator.estimate(image.getvalue()))
        self.assertEqual(result.estimate.meal_name, "rice bowl")
        self.assertEqual(result.model, "gpt-5.4")
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(responses.calls[0]["text"]["format"]["name"], "meal_macros")
        self.assertNotIn("metrics_event_id", _estimate_payload(result.estimate))


if __name__ == "__main__":
    unittest.main()
