#!/usr/bin/env python3
"""Offline-first estimator evaluation; live model calls are explicitly opt-in."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from macro_bot.direct_estimator import DirectOpenAIEstimator, validate_result


def _load(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("nutrition manifest must be a non-empty list")
    return payload


def _pct(value: int, total: int) -> str:
    return "n/a" if not total else f"{100.0 * value / total:.1f}%"


def _structural(payload: dict[str, Any]) -> tuple[bool, str]:
    try:
        validated = validate_result(payload)
        status = str(validated.get("reconciliation_status", ""))
        if status not in {"matched", "reconciled_from_items", "partial_item_breakdown", "reconciliation_required"}:
            return False, "invalid_reconciliation_status"
        return True, status
    except Exception as err:
        return False, type(err).__name__


async def _run_live(cases: list[dict[str, Any]], base: Path) -> list[dict[str, Any]]:
    estimator = DirectOpenAIEstimator()
    results = []
    for case in cases:
        image_path = (base / str(case["image"])).resolve()
        started = time.perf_counter()
        try:
            result = await estimator.estimate(image_path.read_bytes(), caption=str(case.get("caption", "")))
            results.append({"case": case, "payload": result.estimate.to_payload(), "latency_ms": (time.perf_counter() - started) * 1000})
        except Exception as err:
            results.append({"case": case, "error": type(err).__name__, "latency_ms": (time.perf_counter() - started) * 1000})
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="evals/nutrition/manifest.json")
    parser.add_argument("--live", action="store_true", help="make explicit vision-model calls")
    args = parser.parse_args()
    manifest_path = Path(args.manifest).expanduser().resolve()
    cases = _load(manifest_path)
    records = asyncio.run(_run_live(cases, manifest_path.parent)) if args.live else []

    labelled = [case for case in cases if case.get("label_status") == "labelled"]
    valid = 0
    failures = 0
    statuses: dict[str, int] = {}
    latencies = []
    for record in records:
        latencies.append(float(record["latency_ms"]))
        if "payload" not in record:
            failures += 1
            continue
        ok, status = _structural(record["payload"])
        if ok:
            valid += 1
            statuses[status] = statuses.get(status, 0) + 1
        else:
            failures += 1

    print(f"cases: {len(cases)}")
    print(f"labelled_cases: {len(labelled)}")
    print(f"schema_valid: {_pct(valid, len(records)) if args.live else 'n/a (offline manifest only)'}")
    print(f"identification_pass: n/a ({len(labelled)} defensibly labelled cases)")
    print("mean_calorie_error: n/a (no defensible calorie labels)")
    print("mean_protein_error: n/a (no defensible macro labels)")
    print("follow_up_precision_recall: n/a (no labelled follow-up cases)")
    print(f"average_latency_ms: {sum(latencies) / len(latencies):.1f}" if latencies else "average_latency_ms: n/a")
    print(f"failures: {failures}")
    if statuses:
        print("reconciliation_status: " + ", ".join(f"{key}={value}" for key, value in sorted(statuses.items())))
    if not labelled:
        print("coverage_note: add human-reviewed labels before making accuracy claims")


if __name__ == "__main__":
    main()
