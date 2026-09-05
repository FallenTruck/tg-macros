import copy
import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.nutrition_corpus import CorpusDatabase, case_by_id, load_cases, sample_stats, caption_for
from tests.test_direct_estimator import _structured_payload


class NutritionCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = CorpusDatabase(Path(self.temp.name) / "corpus.sqlite3")
        self.addCleanup(self.db.close)
        self.case = case_by_id("astons-all-day-breakfast-001")
        self.db.sync_cases([self.case])
        self.batch = self.db.start_batch(transport="offline_test", context_hash="same-test-context", settings={"repeats": 2})

    def job(self, kcal=500, model="test-model", version="test-v1"):
        payload = _structured_payload(calories=kcal)
        payload["estimator_version"] = version
        payload["reconciliation_status"] = "matched"
        return {"mode": "estimate", "status": "complete", "estimate": payload, "model": model}

    def test_corpus_has_distinct_real_images_and_preserves_user_correction(self):
        cases = load_cases()
        self.assertGreaterEqual(len(cases), 7)
        self.assertEqual(len({case["image_sha256"] for case in cases}), len(cases))
        self.assertGreaterEqual(len({case["food_type"] for case in cases}), 6)
        self.assertIn("mac and cheese", self.case["expected_visible_components"])
        self.assertNotIn("pasta", self.case["caption"])
        self.assertEqual(self.case["known_portions_g"], {})
        self.assertEqual(self.case["acceptable_macro_range"], {})
        for case in cases[1:]:
            self.assertTrue(case["source"]["url"].startswith("https://commons.wikimedia.org/"))
            self.assertTrue(case["source"]["license"])

    def test_variance_is_sample_variance_and_one_repeat_is_not_zero_spread(self):
        self.assertEqual(sample_stats([200])["sample_stddev"], None)
        stats = sample_stats([200, 400])
        self.assertEqual(stats["mean"], 300)
        self.assertAlmostEqual(stats["sample_stddev"], math.sqrt(20_000), places=3)
        self.assertAlmostEqual(stats["cv_pct"], 47.140, places=3)
        self.assertIsNone(sample_stats([0, 0])["cv_pct"])
        self.assertIsNone(sample_stats([])["mean"])

    def test_history_survives_label_edits_and_reopening_database(self):
        self.db.record(self.batch, self.case, "labelled", 1, 1000, job=self.job())
        changed = copy.deepcopy(self.case)
        changed["caption"] = "New reviewed caption"
        self.db.sync_cases([changed])
        other = CorpusDatabase(self.db.path)
        try:
            old = other.report(self.batch)["groups"][0]
            self.assertEqual(old["caption"], self.case["caption"])
            self.assertEqual(old["macros"]["calories"]["n"], 1)
        finally:
            other.close()

    def test_variants_and_versions_are_never_pooled(self):
        self.db.record(self.batch, self.case, "labelled", 1, 100, job=self.job(400))
        self.db.record(self.batch, self.case, "labelled", 2, 100, job=self.job(600))
        self.db.record(self.batch, self.case, "none", 1, 100, job=self.job(900))
        self.db.record(self.batch, self.case, "labelled", 3, 100, job=self.job(200, version="v2"))
        groups = self.db.report(self.batch)["groups"]
        self.assertEqual(len(groups), 3)
        same = next(g for g in groups if g["successes"] == 2)
        self.assertEqual(same["macros"]["calories"]["mean"], 500)
        self.assertEqual(caption_for(self.case, "none"), "")

    def test_server_version_groups_runs_despite_model_generated_version_text(self):
        for repeat, text in enumerate(("v1", "meal-photo-v1"), 1):
            job = self.job(version=text)
            job["usage"] = {"estimator_version": "nutrition-estimator-v2"}
            self.db.record(self.batch, self.case, "labelled", repeat, 100, job=job)
        groups = self.db.report(self.batch)["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["estimator_version"], "nutrition-estimator-v2")
        self.assertEqual(groups[0]["successes"], 2)
        self.assertEqual(groups[0]["reported_estimator_version_counts"], {"v1": 1, "meal-photo-v1": 1})

    def test_failures_are_retained_and_duplicate_runs_cannot_overwrite(self):
        self.db.record(self.batch, self.case, "labelled", 1, 100, error_category="TimeoutError")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.record(self.batch, self.case, "labelled", 1, 100, job=self.job())
        group = self.db.report(self.batch)["groups"][0]
        self.assertEqual(group["failures"], 1)
        self.assertIsNone(group["macros"]["calories"]["mean"])

    def test_log_actions_nonfinite_and_negative_results_are_rejected(self):
        bad = self.job()
        bad["action"] = {"status": "pending"}
        with self.assertRaises(ValueError):
            self.db.record(self.batch, self.case, "labelled", 1, 1, job=bad)
        for value in (float('nan'), -1, True):
            bad = self.job()
            bad["estimate"]["total_best"]["calories"] = value
            with self.assertRaises(ValueError):
                self.db.record(self.batch, self.case, "labelled", 1, 1, job=bad)

    def test_changed_image_bytes_fail_before_any_model_call(self):
        manifest = Path(self.temp.name) / "manifest.json"
        case = copy.deepcopy(self.case)
        from scripts.nutrition_corpus import image_path
        case["image"] = str(image_path(case))
        case["image_sha256"] = "changed"
        manifest.write_text(json.dumps([case]))
        with self.assertRaisesRegex(ValueError, "checksum"):
            load_cases(manifest)


class LiveResultPollingTests(unittest.TestCase):
    def request(self, jobs, status=200):
        from unittest.mock import Mock
        request = Mock()
        request.get.side_effect = [Mock(status=status, json=lambda job=job: job) for job in jobs]
        return request

    def test_polls_exact_uploaded_job_until_complete(self):
        from scripts.nutrition_variance import wait_lab_job
        jobs = [{"job_id": "uploaded-id", "mode": "estimate", "status": status}
                for status in ("queued", "running", "complete")]
        request = self.request(jobs)
        result = wait_lab_job(request, "https://dev.example", "uploaded-id", sleep=lambda _: None)
        self.assertEqual(result, jobs[-1])
        self.assertEqual(request.get.call_count, 3)
        for call in request.get.call_args_list:
            self.assertEqual(call.args[0], "https://dev.example/api/e2e/nutrition-lab/jobs/uploaded-id")

    def test_rejects_wrong_job_log_mode_failures_and_deadline(self):
        from scripts.nutrition_variance import wait_lab_job
        for overrides, error in (({"job_id": "old-job"}, ValueError),
                                 ({"mode": "log"}, ValueError),
                                 ({"status": "failed"}, RuntimeError)):
            with self.subTest(overrides=overrides):
                job = {"job_id": "id", "mode": "estimate", "status": "complete", **overrides}
                with self.assertRaises(error):
                    wait_lab_job(self.request([job]), "https://dev.example", "id")
        with self.assertRaises(RuntimeError):
            wait_lab_job(self.request([{}], status=401), "https://dev.example", "id")
        request = self.request([])
        with self.assertRaises(TimeoutError):
            wait_lab_job(request, "https://dev.example", "id", timeout_seconds=0)
        request.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
