#!/usr/bin/env python3
"""Offline accuracy reports; real deployed Lab calls require explicit --live."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.nutrition_corpus import (CorpusDatabase, DEFAULT_DATABASE, DEFAULT_MANIFEST, MACROS,
                                     ROOT, caption_for, digest, load_cases, sample_stats, summarize_runs)
from scripts.nutrition_groundtruth import (normalize_name, number, require, require_private_output,
                                         validate_ground_truth, write_json)

EXPECTED_VERSION = "nutrition-estimator-v2"
EXPECTED_MODEL = "gpt-5.4"
DEFAULT_REPORT = ROOT / "artifacts/nutrition/accuracy-baseline.json"
PERCENT_MINIMUM = {"calories": 10, "protein_g": 1, "carbs_g": 1, "fat_g": 1, "portion_g": 1}


def errors(actual, estimate, metric):
    number(actual, "actual")
    number(estimate, "estimate")
    error = estimate - actual
    meaningful = actual >= PERCENT_MINIMUM[metric]
    return {"actual": actual, "estimate": estimate, "signed_error": error, "absolute_error": abs(error),
            "percentage_error": 100 * error / actual if meaningful else None,
            "absolute_percentage_error": 100 * abs(error) / actual if meaningful else None}


def range_metrics(actual, low, high):
    number(low, "low")
    number(high, "high")
    require(low <= high, "Invalid estimate interval")
    return {"low": low, "high": high, "hit": low <= actual <= high, "width": high - low}


def match_components(label, estimate):
    """Exact normalized aliases only; collisions/duplicate item matches stay unmatched."""
    items = estimate.get("items", [])
    matched_indices = set()
    matches, portions, missed, forbidden = [], [], [], []
    for component in label["components"]:
        aliases = {normalize_name(n) for n in [component["name"], *component["aliases"]]}
        indices = [i for i, item in enumerate(items) if normalize_name(item["name"]) in aliases]
        safe = len(indices) == 1
        matched_indices.update(indices)
        matches.append({"component": component["name"], "expected_present": component["present"],
                        "major": component["major"], "status": "matched" if safe else "ambiguous" if indices else "unmatched",
                        "items": [items[i]["name"] for i in indices],
                        "actual_count": component.get("consumed_count"),
                        "count_error": None, "count_note": "Estimator has no structured item count" if "consumed_count" in component else None})
        if component["major"] and component["present"] and not indices:
            missed.append(component["name"])
        if component["major"] and not component["present"] and indices:
            forbidden.extend(items[i]["name"] for i in indices)
        if component["present"] and "consumed_weight_g" in component:
            portion = {"component": component["name"], "status": "matched" if safe else "unmatched",
                       "actual_g": component["consumed_weight_g"], "error": None}
            if safe and items[indices[0]].get("portion_g") is not None:
                portion["error"] = errors(component["consumed_weight_g"], items[indices[0]]["portion_g"], "portion_g")
            portions.append(portion)
    # Extra claims require exhaustive labels or an explicit absent-component label.
    # Tiny unmatched items remain unscored to avoid penalizing garnish wording.
    threshold = max(25, .05 * estimate["total_best"]["calories"])
    unknown = [item["name"] for i, item in enumerate(items)
               if i not in matched_indices and item.get("calories", 0) >= threshold]
    expected = [m for m in matches if m["major"] and m["expected_present"]]
    extra = forbidden + (unknown if label["components_complete"] else [])
    return {"matches": matches, "portions": portions, "expected_major_count": len(expected),
            "found_major_count": sum(m["status"] == "matched" for m in expected),
            "ambiguous_major_count": sum(m["status"] == "ambiguous" for m in expected),
            "missed": missed, "unsupported_major": extra,
            "unverified_major": [] if label["components_complete"] else unknown,
            "extras_fully_scoreable": label["components_complete"], "extra_major_threshold_kcal": threshold}


def score_run(row):
    case = json.loads(row["case_snapshot_json"])
    validate_ground_truth(case)
    require(row["image_sha256"] == case["image_sha256"], "Run image hash disagrees with frozen label snapshot")
    require(row["caption"] == caption_for(case, row["variant"]), "Run caption disagrees with its frozen variant")
    gt = case.get("ground_truth")
    job = json.loads(row["result_json"]) if row["result_json"] else None
    result = {"case_id": case["id"], "food_type": case["food_type"], "image_sha256": row["image_sha256"],
              "ground_truth_hash": digest(gt), "ground_truth": gt, "tier": gt["confidence_tier"] if gt else None,
              "variant": row["variant"], "caption": row["caption"], "model": row["model"],
              "estimator_version": row["estimator_version"], "repeat": row["repeat_index"],
              "latency_ms": row["latency_ms"], "error_category": row["error_category"], "macros": {}}
    if not job:
        return result
    estimate = job["estimate"]
    require(job.get("model") == row["model"], "Run model disagrees with structured job")
    require(job.get("mode") == "estimate" and job.get("status") == "complete" and "action" not in job, "Accuracy needs completed estimate-only jobs")
    require(job.get("usage", {}).get("estimator_version") == row["estimator_version"], "Accuracy requires authoritative server version")
    for metric in MACROS:
        best = number(estimate["total_best"][metric], "total_best")
        interval = range_metrics(best, estimate["total_low"][metric], estimate["total_high"][metric])
        require(interval["hit"], "Best estimate must be inside its interval")
        if gt and gt["confidence_tier"] == "A":
            actual = gt["total"][metric]
            result["macros"][metric] = {**errors(actual, best, metric),
                                        "range": range_metrics(actual, interval["low"], interval["high"])}
            if gt.get("uncertainty"):
                lo, hi = gt["uncertainty"]["low"][metric], gt["uncertainty"]["high"][metric]
                result["macros"][metric]["truth_uncertainty"] = {"low": lo, "high": hi,
                    "fully_covered": interval["low"] <= lo <= hi <= interval["high"],
                    "overlaps": interval["low"] <= hi and lo <= interval["high"]}
    result.update(best=estimate["total_best"], low=estimate["total_low"], high=estimate["total_high"],
                  reconciliation_status=estimate.get("reconciliation_status"),
                  analysis_latency_ms=job.get("latency_ms"), follow_up=bool(estimate.get("follow_up_question")),
                  identification=match_components(gt, estimate) if gt else None)
    return result


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def aggregate_errors(case_errors):
    """Equal weight per case; repeats cannot dominate the cross-case baseline."""
    summaries = []
    for case_id, values in case_errors.items():
        if values:
            summaries.append({"case_id": case_id, "mae": mean([v["absolute_error"] for v in values]),
                              "bias": mean([v["signed_error"] for v in values]),
                              "mape": mean([v["absolute_percentage_error"] for v in values]),
                              "percentage_error": mean([v["percentage_error"] for v in values]),
                              "coverage": mean([float(v["range"]["hit"]) for v in values if "range" in v]),
                              "width": mean([v["range"]["width"] for v in values if "range" in v])})
    apes = [v["mape"] for v in summaries if v["mape"] is not None]
    pcts = [v["percentage_error"] for v in summaries if v["percentage_error"] is not None]
    return {"case_count": len(summaries), "scored_estimates": sum(len(v) for v in case_errors.values()),
            "mae": mean([v["mae"] for v in summaries]),
            "median_absolute_error": statistics.median(v["mae"] for v in summaries) if summaries else None,
            "mape": mean(apes), "percentage_case_count": len(apes),
            "median_absolute_percentage_error": statistics.median(apes) if apes else None,
            "median_percentage_error": statistics.median(pcts) if pcts else None,
            "bias": mean([v["bias"] for v in summaries]), "range_coverage": mean([v["coverage"] for v in summaries]),
            "average_interval_width": mean([v["width"] for v in summaries]),
            "worst_case": max(summaries, key=lambda v: v["mae"]) if summaries else None,
            "cases_worst_first": sorted(summaries, key=lambda v: v["mae"], reverse=True)}


def aggregate_group(runs):
    case_ids = sorted({r["case_id"] for r in runs})
    successful = [r for r in runs if "best" in r]
    macro = {m: aggregate_errors({c: [r["macros"][m] for r in successful if r["case_id"] == c and m in r["macros"]]
                                 for c in case_ids}) for m in MACROS}
    portion = aggregate_errors({c: [p["error"] for r in successful if r["case_id"] == c and r.get("identification")
                                    for p in r["identification"]["portions"] if p["error"]] for c in case_ids})
    identities = [r["identification"] for r in successful if r.get("identification")]
    count = sum(i["expected_major_count"] for i in identities)
    complete = [i for i in identities if i["extras_fully_scoreable"]]
    return {"cases": len(case_ids), "attempts": len(runs), "successes": len(successful), "macros": macro,
            "portions_g": portion,
            "estimated_interval_widths": {m: sample_stats([r["high"][m] - r["low"][m] for r in successful]) for m in MACROS},
            "identification": {"expected_major_exposures": count,
                "missed_count": sum(len(i["missed"]) for i in identities),
                "miss_rate": sum(len(i["missed"]) for i in identities)/count if count else None,
                "found_count": sum(i["found_major_count"] for i in identities),
                "ambiguous_count": sum(i["ambiguous_major_count"] for i in identities),
                "unsupported_major_count": sum(len(i["unsupported_major"]) for i in identities),
                "complete_label_estimates": len(complete),
                "extra_meal_rate": mean([float(bool(i["unsupported_major"])) for i in complete]),
                "unverified_major_count": sum(len(i["unverified_major"]) for i in identities)},
            "follow_up_frequency": mean([float(r["follow_up"]) for r in successful]),
            "latency_ms": sample_stats([r["latency_ms"] for r in successful]),
            "reconciliation_counts": dict(Counter(r["reconciliation_status"] for r in successful))}


def build_report(db, batch_id):
    repeatability = db.report(batch_id)
    require(repeatability.get("batch_id"), "No batch exists")
    require(repeatability["status"] in {"complete", "complete_with_errors"}, "Invalid or interrupted batch cannot support accuracy claims")
    if repeatability["transport"] == "dev_browser_lab":
        require(repeatability["settings"].get("domain_unchanged") is True and repeatability["settings"].get("logout_verified") is True,
                "Live accuracy requires verified unchanged domain and logout evidence")
    rows = [dict(r) for r in db.connection.execute("SELECT * FROM runs WHERE batch_id=? ORDER BY case_id, variant, repeat_index", (repeatability["batch_id"],))]
    runs = [score_run(row) for row in rows]
    for case_id in {r["case_id"] for r in runs}:
        require(len({(r["image_sha256"], r["ground_truth_hash"]) for r in runs if r["case_id"] == case_id}) == 1,
                "Ground truth and image must remain identical across caption variants")
    grouped = defaultdict(list)
    for r in runs:
        grouped[(r["model"], r["estimator_version"], r["variant"])].append(r)
    # Prevent changed truths/images/captions within a case/variant from pooling.
    for entries in grouped.values():
        for case_id in {r["case_id"] for r in entries}:
            require(len({(r["image_sha256"], r["ground_truth_hash"], r["caption"]) for r in entries if r["case_id"] == case_id}) == 1,
                    "Changed image/label/caption within an accuracy group")
    groups = [{"model": key[0], "estimator_version": key[1], "variant": key[2], **aggregate_group(entries)} for key, entries in grouped.items()]
    comparisons = []
    for model, version in sorted({(r["model"], r["estimator_version"]) for r in runs if r["model"]}):
        for a, b in combinations(sorted({r["variant"] for r in runs if r["model"] == model and r["estimator_version"] == version}), 2):
            entries = [r for r in runs if r["model"] == model and r["estimator_version"] == version and "best" in r]
            identity = lambda r: (r["case_id"], r["image_sha256"], r["ground_truth_hash"])
            shared = {identity(r) for r in entries if r["variant"] == a} & {identity(r) for r in entries if r["variant"] == b}
            sides = {v: aggregate_group([r for r in entries if r["variant"] == v and identity(r) in shared]) for v in (a, b)}
            comparisons.append({"model": model, "estimator_version": version, "variants": [a, b],
                                "paired_case_count": len(shared), "paired_case_ids": sorted(x[0] for x in shared), "results": sides,
                                "mae_delta_second_minus_first": {m: sides[b]["macros"][m]["mae"] - sides[a]["macros"][m]["mae"]
                                    if sides[b]["macros"][m]["mae"] is not None and sides[a]["macros"][m]["mae"] is not None else None for m in MACROS}})
    unique_cases = {r["case_id"]: r for r in runs}
    observations = []
    for group in groups:
        identity = group["identification"]
        if identity["missed_count"]:
            observations.append({"kind": "unmatched_expected_labels", "variant": group["variant"],
                                 "model": group["model"], "estimator_version": group["estimator_version"],
                                 "missed_count": identity["missed_count"], "expected_major_exposures": identity["expected_major_exposures"],
                                 "miss_rate": identity["miss_rate"],
                                 "interpretation": "Reported item names did not match frozen aliases; human review needed before attributing food misidentification."})
        for metric, scores in group["macros"].items():
            if scores["range_coverage"] is not None and scores["range_coverage"] < 1:
                observations.append({"kind": "range_misses", "metric": metric, "variant": group["variant"],
                                     "model": group["model"], "estimator_version": group["estimator_version"],
                                     "coverage": scores["range_coverage"], "case_count": scores["case_count"]})
            if scores["mape"] is not None:
                observations.append({"kind": "macro_error", "metric": metric, "variant": group["variant"],
                                     "model": group["model"], "estimator_version": group["estimator_version"],
                                     "mape": scores["mape"], "mae": scores["mae"], "bias": scores["bias"],
                                     "case_count": scores["case_count"], "worst_case": scores["worst_case"]})
    low_variance_high_bias = []
    for g in repeatability["groups"]:
        entries = [r for r in runs if r["case_id"] == g["case_id"] and r["variant"] == g["variant"] and r["model"] == g["model"] and r["estimator_version"] == g["estimator_version"]]
        for m in MACROS:
            pct = mean([r["macros"][m]["percentage_error"] for r in entries if m in r["macros"]])
            cv = g["macros"][m]["cv_pct"]
            if cv is not None and cv <= 10 and pct is not None and abs(pct) >= 20:
                low_variance_high_bias.append({"case_id": g["case_id"], "variant": g["variant"], "metric": m, "cv_pct": cv, "bias_pct": pct})
    return {"schema_version": 1, "batch_id": repeatability["batch_id"], "created_at": repeatability["created_at"],
            "report_source_hashes": {name: digest((ROOT / "scripts" / name).read_bytes()) for name in
                ("nutrition_accuracy.py", "nutrition_groundtruth.py", "nutrition_corpus.py")},
            "status": repeatability["status"], "transport": repeatability["transport"], "settings": repeatability["settings"],
            "context_hash": repeatability["context_hash"], "attempts": len(runs),
            "labelled_cases_by_tier": {t: sum(r["tier"] == t for r in unique_cases.values()) for t in ("A", "B", "C")},
            "foods": dict(Counter(r["food_type"] for r in unique_cases.values())),
            "groups": groups, "caption_comparisons": comparisons, "runs": runs, "repeatability": repeatability["groups"],
            "low_variance_high_bias": low_variance_high_bias,
            "observed_weaknesses": sorted(observations, key=lambda o: (o["kind"],
                -o.get("miss_rate", 0) if o["kind"] == "unmatched_expected_labels" else o.get("coverage", 1) if o["kind"] == "range_misses" else -o.get("mape", 0))),
            "ranking_rule": "Separate evidence families: descending macro MAPE, ascending range coverage, descending unmatched-label rate. No combined accuracy/calibration score.",
            "human_failure_reviews": [{"case_id": r["case_id"], **r["ground_truth"]["review"]} for r in unique_cases.values() if r["ground_truth"] and r["ground_truth"].get("review")],
            "limitations": ["Repeatability is not accuracy. No estimator tuning was performed.",
                "Only Tier A totals support macro error and interval coverage; absent metrics are null, never zero.",
                "Percent errors excluded below 10 kcal or 1 g actual; inclusive ranges are descriptive, not nominal confidence intervals.",
                "Aggregate errors weight each case equally using mean per-repeat errors; medians are across these case means.",
                "Identity checks score exact annotated aliases in reported items. Unmatched labels may be wording differences; no fuzzy self-grading.",
                "Extra rates require exhaustive labels. Unverified major components are not called hallucinations.",
                "Structured item counts are unavailable; known counts do not imply edible grams or macro truth.",
                "No clean prior toggle exists in the deployed Lab; synthetic context is held fixed, with no source-user priors imported.",
                "User-partition hashes prove unchanged domain state, not absence of transient writes. Offline isolation tests cover zero estimate writes; login/logout create/delete session records."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--variant", action="append", choices=("none", "generic", "labelled", "fact-rich"))
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--batch", help="Rebuild a report from frozen database snapshots, offline")
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    require_private_output(args.output)
    require_private_output(args.database)
    require(not (args.live and args.batch), "Choose live or an existing batch")
    if not args.live and not args.batch:
        if args.output.exists():
            report = json.loads(args.output.read_text())
            print(json.dumps({k: report[k] for k in ("batch_id", "status", "attempts", "labelled_cases_by_tier", "groups")}, indent=2))
        else:
            print("No accuracy baseline yet. Supply --batch to score history or --live with explicit --case selections.")
        return 0
    require(not args.output.exists(), "Frozen report already exists; choose a new --output path before any live calls")
    require(1 <= args.repeats <= 20, "--repeats must be between 1 and 20")
    db = CorpusDatabase(args.database)
    try:
        if args.live:
            require(bool(args.case_ids), "Live accuracy requires explicit --case selections")
            all_cases = load_cases(args.manifest)
            require(set(args.case_ids) <= {c["id"] for c in all_cases}, "Unknown case id")
            cases = [c for c in all_cases if c["id"] in args.case_ids]
            require(all(c.get("ground_truth") for c in cases), "Accuracy calls require defensible labels")
            variants = list(dict.fromkeys(args.variant or ["none", "generic"]))
            for c in cases:
                for v in variants:
                    caption_for(c, v)
            print(f"Planned: {len(cases)*len(variants)*args.repeats} real estimate-only Lab calls; {EXPECTED_VERSION} / {EXPECTED_MODEL}", flush=True)
            db.sync_cases(cases)
            from scripts.nutrition_variance import run_live
            batch_id = run_live(db, cases, manifest=args.manifest, repeats=args.repeats, variants=variants,
                                expected_model=EXPECTED_MODEL, expected_version=EXPECTED_VERSION)
        else:
            batch_id = args.batch
        report = build_report(db, batch_id)
        require(not args.output.exists(), "Frozen report already exists; choose a new --output path")
        write_json(args.output, report)
        print(json.dumps({k: report[k] for k in ("batch_id", "status", "attempts", "labelled_cases_by_tier")}, indent=2))
        print(f"Report: {args.output}")
        return 0 if report["status"] == "complete" else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
