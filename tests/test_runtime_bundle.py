import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "lambda_handlers" / "runtime"


class RuntimeBundleTests(unittest.TestCase):
    def test_runtime_bundle_matches_canonical_serverless_sources(self):
        module_names = [
            "direct_estimator.py",
            "formatting.py",
            "models.py",
            "profile_targets.py",
            "recommendations.py",
            "serverless_auth.py",
            "serverless_data.py",
            "serverless_service.py",
            "workout_execution.py",
            "workout_programme.py",
        ]
        for name in module_names:
            self.assertEqual(
                (ROOT / "macro_bot" / name).read_bytes(),
                (RUNTIME / "macro_bot" / name).read_bytes(),
                name,
            )
        self.assertEqual((ROOT / "lambda_handlers" / "api.py").read_bytes(), (RUNTIME / "api.py").read_bytes())
        self.assertEqual((ROOT / "lambda_handlers" / "worker.py").read_bytes(), (RUNTIME / "worker.py").read_bytes())
        self.assertEqual((ROOT / "food_catalog.json").read_bytes(), (RUNTIME / "food_catalog.json").read_bytes())

    def test_runtime_bundle_contains_no_repository_data_or_test_sources(self):
        forbidden = {"metrics", "tests", "images", ".venv", ".git", "macro_api.py", "meals.csv", "meals_v2.csv", "user_profiles.json"}
        found = {
            path.name
            for path in RUNTIME.rglob("*")
            if path.name in forbidden or path.suffix in {".csv", ".jsonl"}
        }
        self.assertEqual(found, set())


if __name__ == "__main__":
    unittest.main()
