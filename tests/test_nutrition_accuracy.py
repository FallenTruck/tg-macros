"""Synthetic arithmetic fixtures only. These are not real meal ground truth."""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from scripts.nutrition_accuracy import (aggregate_errors, build_report, errors, main, match_components,
                                        range_metrics, score_run)
from scripts.nutrition_corpus import CorpusDatabase, ROOT, caption_for, case_by_id, digest, image_path, load_cases
from scripts.nutrition_groundtruth import (fact_rich_caption, prepare_ground_truth, require_private_output,
                                          validate_ground_truth)
from scripts.nutrition_label import apply_label


def label_fixture(tier="A"):
    c = {"name": "rice", "aliases": ["cooked rice"], "major": True, "present": True}
    label = {"schema_version": 1, "confidence_tier": tier, "source_type": "weighed_meal" if tier == "A" else "component_facts",
             "provenance": {"date": "2026-09-05", "source": "unit-test human annotation", "method": "human_measurement", "reference": "synthetic test worksheet, not a corpus label"},
             "values_kind": "derived" if tier == "A" else "facts", "components_complete": tier == "A", "components": [c]}
    if tier in {"A", "B"}:
        c["consumed_weight_g"] = 180
    if tier == "A":
        c["nutrition_reference"] = {"basis": "per_100g", "source": "synthetic arithmetic reference",
                                    "macros": {"calories": 130, "protein_g": 2, "carbs_g": 29, "fat_g": 1}}
        label["derive_total"] = True
    return prepare_ground_truth(label)


def case_fixture(tier="A"):
    case = copy.deepcopy(case_by_id("astons-all-day-breakfast-001"))
    case.update(label_status="ground_truth", ground_truth=label_fixture(tier), generic_caption="rice")
    return case


def job_fixture(kcal=250):
    best = {"calories": kcal, "protein_g": 4, "carbs_g": 54, "fat_g": 2}
    return {"mode": "estimate", "status": "complete", "model": "gpt-5.4", "usage": {"estimator_version": "nutrition-estimator-v2"},
            "latency_ms": 800, "estimate": {"total_best": best,
                "total_low": {k: v * .8 for k, v in best.items()}, "total_high": {k: v * 1.2 for k, v in best.items()},
                "items": [{"name": "cooked rice", "portion_g": 200, "calories": kcal}], "reconciliation_status": "matched"}}


class GroundTruthTests(unittest.TestCase):
    def test_tiers_and_missing_numeric_truth(self):
        for tier in "ABC":
            validate_ground_truth(case_fixture(tier))
        c = case_fixture("C")
        c["ground_truth"]["total"] = label_fixture()["total"]
        with self.assertRaises(ValueError): validate_ground_truth(c)
        c = case_fixture("B")
        del c["ground_truth"]["components"][0]["consumed_weight_g"]
        with self.assertRaises(ValueError): validate_ground_truth(c)

    def test_schema_rejects_model_payloads_and_unknown_fields(self):
        for change in ({"confidence_tier": "D"}, {"schema_version": True}, {"model": "gpt-5.4"}, {"source_type": "model_estimate"}):
            c = case_fixture()
            c["ground_truth"].update(change)
            with self.assertRaises(ValueError): validate_ground_truth(c)

    def test_provenance_is_required_and_model_annotation_is_invalid(self):
        for key in ("source", "reference", "date"):
            c = case_fixture()
            c["ground_truth"]["provenance"][key] = ""
            with self.assertRaises(ValueError): validate_ground_truth(c)
        c = case_fixture()
        c["ground_truth"]["provenance"]["method"] = "llm"
        with self.assertRaises(ValueError): validate_ground_truth(c)

    def test_per_100g_arithmetic_persists_reference_and_derivation(self):
        label = label_fixture()
        self.assertEqual(label["total"]["calories"], 234)
        c = label["components"][0]
        self.assertEqual(c["nutrition_reference"]["macros"]["calories"], 130)
        self.assertEqual(c["derivation"]["factor"], 1.8)
        self.assertEqual(c["derivation"]["expression"], "180 g / 100 g")

    def test_per_serving_arithmetic_and_no_double_fraction(self):
        label = label_fixture("B")
        c = label["components"][0]
        c.pop("consumed_weight_g")
        c.update(consumed_servings=.5, consumed_fraction=.5,
                 nutrition_reference={"source": "test package", "basis": "per_serving", "macros": label_fixture()["total"]})
        c = prepare_ground_truth(label)["components"][0]
        self.assertEqual(c["nutrition"]["calories"], 117)
        c.pop("consumed_servings")
        c["consumed_weight_g"] = 90
        c["nutrition_reference"]["serving_weight_g"] = 180
        label["components"] = [c]
        self.assertEqual(prepare_ground_truth(label)["components"][0]["nutrition"]["calories"], 117)

    def test_unknown_quantity_cannot_derive_nutrition(self):
        label = label_fixture()
        del label["components"][0]["consumed_weight_g"]
        with self.assertRaises(ValueError): prepare_ground_truth(label)

    def test_conflicting_serving_weight_and_servings_fail(self):
        label = label_fixture()
        c = label["components"][0]
        c["consumed_servings"] = 2
        c["nutrition_reference"]["serving_weight_g"] = 100
        with self.assertRaises(ValueError): prepare_ground_truth(label)

    def test_exact_product_supports_tier_b_but_not_numeric_truth(self):
        c = case_fixture("B")
        c["ground_truth"]["components"][0].pop("consumed_weight_g")
        c["ground_truth"]["components"][0]["product"] = "Known test product"
        validate_ground_truth(c)
        self.assertEqual(fact_rich_caption(c), "Known test product")
        self.assertNotIn("total", c["ground_truth"])

    def test_official_supplied_totals_are_not_overwritten(self):
        label = label_fixture()
        label["total"]["calories"] = 235
        self.assertEqual(prepare_ground_truth(label)["total"]["calories"], 235)
        label["total"]["calories"] = 999
        with self.assertRaises(ValueError): prepare_ground_truth(label)

    def test_portions_nonfinite_negative_boolean_and_fraction_fail(self):
        for key, value in (("consumed_weight_g", 0), ("consumed_count", -1), ("consumed_weight_g", True),
                           ("consumed_weight_g", float("nan")), ("consumed_fraction", 1.1)):
            c = case_fixture("B")
            c["ground_truth"]["components"][0][key] = value
            with self.assertRaises(ValueError): validate_ground_truth(c)

    def test_macro_consistency_component_sums_and_stale_derivation(self):
        for location in ("total", "nutrition", "derivation"):
            c = case_fixture()
            if location == "total": c["ground_truth"]["total"]["calories"] = 900
            elif location == "nutrition": c["ground_truth"]["components"][0]["nutrition"]["protein_g"] = -1
            else: c["ground_truth"]["components"][0]["derivation"]["factor"] = 9
            with self.assertRaises(ValueError): validate_ground_truth(c)

    def test_overlapping_aliases_are_rejected(self):
        c = case_fixture("C")
        c["ground_truth"]["components"].append({"name": "noodles", "aliases": [" COOKED  RICE "], "major": True, "present": True})
        with self.assertRaises(ValueError): validate_ground_truth(c)

    def test_fact_caption_contains_only_annotated_facts_and_no_totals(self):
        self.assertEqual(fact_rich_caption(case_fixture()), "180g rice")
        c = case_fixture()
        self.assertNotIn("234", fact_rich_caption(c))
        c["ground_truth"]["components"][0]["name"] = "234 calories rice"
        with self.assertRaises(ValueError): fact_rich_caption(c)
        self.assertEqual(fact_rich_caption(case_fixture("C")), "rice")

    def test_uncertainty_must_enclose_actual(self):
        c = case_fixture()
        total = c["ground_truth"]["total"]
        c["ground_truth"]["uncertainty"] = {"source": "test scale", "low": total.copy(), "high": total.copy()}
        validate_ground_truth(c)
        c["ground_truth"]["uncertainty"]["low"]["calories"] += 1
        with self.assertRaises(ValueError): validate_ground_truth(c)

    def test_private_paths_stay_ignored(self):
        for path in ("artifacts/nutrition/private/manifest.json", "artifacts/nutrition/private/report.json", "artifacts/nutrition/corpus.sqlite3"):
            self.assertEqual(subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode, 0)
        with self.assertRaises(ValueError): require_private_output(ROOT / "evals/nutrition/private.json")

    def test_private_case_cannot_be_copied_into_public_manifest_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            c = case_fixture("B")
            c["image"] = str(image_path(c))
            c["source"] = {"kind": "authorized_private_telegram_submission"}
            manifest = Path(directory)/"manifest.json"
            manifest.write_text(json.dumps([c]))
            with self.assertRaises(ValueError): load_cases(manifest)
            db = CorpusDatabase(Path(directory)/"outside-private.sqlite3")
            try:
                with self.assertRaises(ValueError): db.sync_cases([c])
            finally:
                db.close()

    def test_label_entry_preserves_other_cases_and_rejects_model_results(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory)/"manifest.json"
            original = case_fixture("C")
            original["image"] = str(image_path(original))
            second = copy.deepcopy(original)
            second["id"] = "untouched"
            manifest.write_text(json.dumps([original, second]))
            result = apply_label(manifest, original["id"], {"ground_truth": label_fixture("B")})
            self.assertEqual(json.loads(manifest.read_text())[1], second)
            self.assertEqual(result["label_history"][-1]["previous"]["ground_truth"], original["ground_truth"])
            before = manifest.read_bytes()
            with self.assertRaises(ValueError): apply_label(manifest, original["id"], job_fixture())
            self.assertEqual(manifest.read_bytes(), before)
            original["image_sha256"] = "bad"
            manifest.write_text(json.dumps([original]))
            with self.assertRaisesRegex(ValueError, "checksum"): apply_label(manifest, original["id"], {"ground_truth": label_fixture()})


class AccuracyMetricTests(unittest.TestCase):
    def test_errors_and_zero_denominator(self):
        e = errors(200, 250, "calories")
        self.assertEqual((e["signed_error"], e["absolute_error"], e["percentage_error"]), (50, 50, 25))
        self.assertEqual(errors(200, 150, "calories")["percentage_error"], -25)
        self.assertIsNone(errors(0, 10, "fat_g")["percentage_error"])
        self.assertIsNone(errors(.2, 10, "fat_g")["absolute_percentage_error"])

    def test_range_coverage_width_and_boundaries(self):
        self.assertEqual(range_metrics(200, 200, 300), {"low": 200, "high": 300, "hit": True, "width": 100})
        self.assertFalse(range_metrics(199, 200, 300)["hit"])
        with self.assertRaises(ValueError): range_metrics(200, 300, 200)

    def test_equal_case_weight_outliers_and_bias(self):
        report = aggregate_errors({"a": [errors(100, 110, "calories")] * 10, "b": [errors(100, 50, "calories")]})
        self.assertEqual(report["mae"], 30)
        self.assertEqual(report["bias"], -20)
        self.assertEqual(report["median_absolute_error"], 30)
        self.assertEqual(report["mape"], 30)
        self.assertEqual(report["worst_case"]["case_id"], "b")

    def test_alias_match_portion_and_ambiguous_item_handling(self):
        label = label_fixture("B")
        estimate = job_fixture()["estimate"]
        result = match_components(label, estimate)
        self.assertEqual(result["portions"][0]["error"]["signed_error"], 20)
        estimate["items"].append(copy.deepcopy(estimate["items"][0]))
        result = match_components(label, estimate)
        self.assertEqual(result["matches"][0]["status"], "ambiguous")
        self.assertIsNone(result["portions"][0]["error"])

    def test_unmatched_not_fuzzy_and_minor_garnish_not_extra(self):
        label = label_fixture("B")
        estimate = job_fixture()["estimate"]
        estimate["items"] = [{"name": "rice with sauce", "portion_g": 200, "calories": 250}, {"name": "parsley", "calories": 1}]
        result = match_components(label, estimate)
        self.assertEqual(result["missed"], ["rice"])
        self.assertEqual(result["unsupported_major"], [])
        self.assertEqual(result["unverified_major"], ["rice with sauce"])
        label["components_complete"] = True
        self.assertEqual(match_components(label, estimate)["unsupported_major"], ["rice with sauce"])

    def test_explicit_absence_can_score_extra_without_exhaustive_labels(self):
        label = label_fixture("C")
        label["components"][0]["present"] = False
        result = match_components(label, job_fixture()["estimate"])
        self.assertEqual(result["unsupported_major"], ["cooked rice"])

    def test_count_never_becomes_grams_or_inferred_macros(self):
        c = case_fixture("B")
        comp = c["ground_truth"]["components"][0]
        comp.pop("consumed_weight_g")
        comp.update(consumed_count=2, count_unit="pieces")
        result = match_components(c["ground_truth"], job_fixture()["estimate"])
        self.assertEqual(result["portions"], [])
        self.assertIsNone(result["matches"][0]["count_error"])

    def test_full_report_groups_caption_variants_versions_and_truth(self):
        with tempfile.TemporaryDirectory() as directory:
            db = CorpusDatabase(Path(directory)/"test.sqlite3")
            self.addCleanup(db.close)
            c = case_fixture()
            db.sync_cases([c])
            batch = db.start_batch(transport="offline_test", context_hash="fixed", settings={})
            for variant in ("none", "generic", "fact-rich"):
                for repeat in (1, 2):
                    db.record(batch, c, variant, repeat, 1000, job=job_fixture())
            db.finish(batch)
            report = build_report(db, batch)
            self.assertEqual(len(report["groups"]), 3)
            self.assertEqual(len(report["caption_comparisons"]), 3)
            self.assertEqual(report["groups"][0]["macros"]["calories"]["mae"], 16)
            self.assertEqual(report["groups"][0]["macros"]["calories"]["range_coverage"], 1)
            self.assertEqual(report["groups"][0]["macros"]["calories"]["average_interval_width"], 100)
            self.assertEqual(report["groups"][0]["estimated_interval_widths"]["calories"]["mean"], 100)
            self.assertEqual({r["ground_truth_hash"] for r in report["runs"]}, {digest(c["ground_truth"])})
            self.assertEqual({r["caption"] for r in report["runs"]}, {"", "rice", "180g rice"})
            changed = copy.deepcopy(c)
            changed["ground_truth"]["notes"] = "changed label"
            db.record(batch, changed, "none", 3, 1000, job=job_fixture())
            with self.assertRaises(ValueError): build_report(db, batch)

    def test_tier_b_report_has_no_macro_claims(self):
        c = case_fixture("B")
        row = {"case_snapshot_json": json.dumps(c), "result_json": json.dumps(job_fixture()), "case_id": c["id"],
               "image_sha256": c["image_sha256"], "variant": "none", "caption": "", "model": "gpt-5.4",
               "estimator_version": "nutrition-estimator-v2", "repeat_index": 1, "latency_ms": 1000, "error_category": None}
        result = score_run(row)
        self.assertEqual(result["macros"], {})
        self.assertTrue(result["identification"])
        row["estimator_version"] = "invented version"
        with self.assertRaises(ValueError): score_run(row)

    def test_normal_accuracy_entry_point_does_not_run_live(self):
        with patch("scripts.nutrition_variance.run_live") as live, patch("scripts.nutrition_accuracy.Path.exists", return_value=False):
            main([])
            live.assert_not_called()

    def test_wrong_deployed_version_is_rejected(self):
        from scripts.nutrition_variance import EstimatorMismatch, check_estimator
        check_estimator(job_fixture(), "gpt-5.4", "nutrition-estimator-v2")
        with self.assertRaises(EstimatorMismatch): check_estimator(job_fixture(), "other", "nutrition-estimator-v2")

    def test_failed_attempts_stay_visible_and_invalid_context_cannot_score(self):
        with tempfile.TemporaryDirectory() as directory:
            db = CorpusDatabase(Path(directory)/"test.sqlite3")
            self.addCleanup(db.close)
            c = case_fixture()
            db.sync_cases([c])
            batch = db.start_batch(transport="offline_test", context_hash="fixed", settings={})
            db.record(batch, c, "none", 1, 1000, error_category="TimeoutError")
            db.finish(batch, "complete_with_errors")
            report = build_report(db, batch)
            self.assertEqual(report["groups"][0]["successes"], 0)
            self.assertIsNone(report["groups"][0]["macros"]["calories"]["mae"])
            db.finish(batch, "invalid_context_domain_changed")
            with self.assertRaises(ValueError): build_report(db, batch)


class LiveRunnerBoundaryTests(unittest.TestCase):
    def exercise(self, *, wrong_version=False, changed=False, job_error=False):
        from contextlib import ExitStack
        from scripts.nutrition_variance import run_live
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            db = CorpusDatabase(Path(directory)/"test.sqlite3")
            stack.callback(db.close)
            c = case_fixture("B")
            db.sync_cases([c])
            pw = MagicMock()
            context = pw.chromium.launch.return_value.new_context.return_value
            page = context.new_page.return_value
            submitted = page.expect_response.return_value.__enter__.return_value
            submitted.value.status = 202
            submitted.value.json.return_value = {"job_id": "test-job"}
            context.request.post.return_value.status = 200
            context.request.get.return_value.status = 200
            context.request.get.return_value.json.return_value = {"authenticated": False}
            stack.enter_context(patch("playwright.sync_api.sync_playwright")).return_value.__enter__.return_value = pw
            resources = stack.enter_context(patch("scripts.e2e_support.dev_resources"))
            resources.return_value = (Mock(), Mock(), {"MiniAppUrl": "https://dev.example"}, Mock())
            for name in ("read_e2e_records", "validate_e2e_credential"):
                stack.enter_context(patch("scripts.e2e_support." + name))
            stack.enter_context(patch("scripts.e2e_support.load_e2e_credentials", return_value=("javaan-e2e", "offline-test-placeholder")))
            snapshots = stack.enter_context(patch("scripts.e2e_support.user_partition_items"))
            snapshots.side_effect = [[{"SK": "PROFILE", "value": 1}], [{"SK": "PROFILE", "value": 2 if changed else 1}]]
            stack.enter_context(patch("scripts.nutrition_variance.open_ready_lab"))
            job = job_fixture()
            if wrong_version: job["model"] = "unexpected"
            polling = stack.enter_context(patch("scripts.nutrition_variance.wait_lab_job", return_value=job))
            if job_error: polling.side_effect = TimeoutError()
            if wrong_version or changed:
                with self.assertRaises((ValueError, RuntimeError)):
                    run_live(db, [c], manifest=ROOT/"evals/nutrition/manifest.json", repeats=1, variants=["none"], expected_model="gpt-5.4", expected_version="nutrition-estimator-v2")
            else:
                run_live(db, [c], manifest=ROOT/"evals/nutrition/manifest.json", repeats=1, variants=["none"], expected_model="gpt-5.4", expected_version="nutrition-estimator-v2")
            report = db.report()
            context.request.post.assert_called_once_with("https://dev.example/api/auth/logout", headers={"Origin": "https://dev.example"}, timeout=30_000)
            self.assertTrue(report["settings"]["logout_verified"])
            self.assertEqual(report["settings"]["domain_unchanged"], not changed)
            self.assertEqual(snapshots.call_count, 2)
            page.get_by_test_id.assert_any_call("nutrition-lab-mode")
            page.get_by_test_id.return_value.select_option.assert_called_once_with("estimate")
            self.assertEqual(report["attempts"], 1)
            return report

    def test_live_transport_records_isolation_evidence_and_logout(self):
        self.assertEqual(self.exercise()["status"], "complete")

    def test_estimator_mismatch_stops_retains_raw_job_and_logs_out(self):
        self.assertEqual(self.exercise(wrong_version=True)["status"], "invalid_estimator_version")

    def test_domain_change_invalidates_even_after_success(self):
        self.assertEqual(self.exercise(changed=True)["status"], "invalid_context_domain_changed")

    def test_failed_job_also_checks_domain_and_logs_out(self):
        self.assertEqual(self.exercise(job_error=True)["status"], "complete_with_errors")


if __name__ == "__main__":
    unittest.main()
