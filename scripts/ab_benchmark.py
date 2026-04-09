#!/usr/bin/env python3
import argparse
import csv
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run repeated /estimate benchmark requests.")
    parser.add_argument("--manifest", required=True, help="CSV with columns: image_path,caption,repeats(optional)")
    parser.add_argument("--api-url", default="http://127.0.0.1:9000/estimate", help="FastAPI /estimate URL")
    parser.add_argument("--experiment-id", required=True, help="Experiment label to tag metrics")
    parser.add_argument("--model", default="", help="Optional model override, e.g. gpt-4.1-mini")
    parser.add_argument("--vision-detail", choices=["low", "high"], default="", help="Optional vision detail override")
    parser.add_argument("--max-side", type=int, default=0, help="Optional max-side override")
    parser.add_argument("--timeout", type=float, default=120.0, help="Request timeout seconds")
    parser.add_argument("--output", default="", help="Output JSONL path (default: metrics/ab_runs_<experiment_id>.jsonl)")
    return parser.parse_args()


def load_manifest(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    required = {"image_path", "caption"}
    if not rows:
        raise ValueError("Manifest is empty")

    missing = required - set(rows[0].keys())
    if missing:
        raise ValueError(f"Manifest missing required columns: {', '.join(sorted(missing))}")

    normalized = []
    for row in rows:
        repeats_raw = (row.get("repeats") or "1").strip()
        repeats = int(repeats_raw) if repeats_raw else 1
        if repeats < 1:
            repeats = 1
        normalized.append(
            {
                "image_path": row["image_path"].strip(),
                "caption": row["caption"],
                "repeats": repeats,
            }
        )
    return normalized


def summarize_successes(results: List[Dict[str, object]]) -> str:
    ok = [r for r in results if r.get("status_code") == 200 and isinstance(r.get("response"), dict)]
    if not ok:
        return "No successful responses to summarize."

    def col(path: List[str]) -> List[float]:
        out = []
        for row in ok:
            node = row["response"]
            for key in path:
                node = node[key]
            out.append(float(node))
        return out

    calories = col(["calories"])
    proteins = col(["protein_g"])
    carbs = col(["carbs_g"])
    fats = col(["fat_g"])
    low_kcal = col(["total_low", "calories"])
    high_kcal = col(["total_high", "calories"])
    latency_ms = [float(r["latency_ms"]) for r in ok]

    def fmt(name: str, arr: List[float]) -> str:
        sd = statistics.pstdev(arr) if len(arr) > 1 else 0.0
        return f"{name}: mean={statistics.mean(arr):.2f}, sd={sd:.2f}, min={min(arr):.2f}, max={max(arr):.2f}"

    lines = [
        f"successful_runs={len(ok)}",
        fmt("calories", calories),
        fmt("protein_g", proteins),
        fmt("carbs_g", carbs),
        fmt("fat_g", fats),
        fmt("low_kcal", low_kcal),
        fmt("high_kcal", high_kcal),
        fmt("latency_ms", latency_ms),
    ]
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest_rows = load_manifest(manifest_path)

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else (Path.cwd() / "metrics" / f"ab_runs_{args.experiment_id}.jsonl")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    results: List[Dict[str, object]] = []

    with httpx.Client(timeout=args.timeout) as client:
        for row in manifest_rows:
            image_path = Path(row["image_path"]).expanduser()
            if not image_path.is_absolute():
                image_path = (manifest_path.parent / image_path).resolve()

            if not image_path.exists():
                raise FileNotFoundError(f"Image not found: {image_path}")

            for run_index in range(1, int(row["repeats"]) + 1):
                form_data = {
                    "caption": row["caption"],
                    "experiment_id": args.experiment_id,
                }
                if args.model:
                    form_data["model_override"] = args.model
                if args.vision_detail:
                    form_data["vision_detail_override"] = args.vision_detail
                if args.max_side > 0:
                    form_data["max_side_override"] = str(args.max_side)

                with image_path.open("rb") as image_file:
                    files = {
                        "file": (
                            image_path.name,
                            image_file.read(),
                            "image/jpeg",
                        )
                    }

                started = time.perf_counter()
                response = client.post(args.api_url, files=files, data=form_data)
                latency_ms = (time.perf_counter() - started) * 1000.0

                payload: object
                try:
                    payload = response.json()
                except Exception:
                    payload = {"raw_text": response.text[:500]}

                record: Dict[str, object] = {
                    "timestamp_utc": datetime.utcnow().isoformat(timespec="seconds"),
                    "experiment_id": args.experiment_id,
                    "image_path": str(image_path),
                    "caption": row["caption"],
                    "run_index": run_index,
                    "status_code": response.status_code,
                    "latency_ms": round(latency_ms, 2),
                    "response": payload,
                }
                results.append(record)
                with output_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=True))
                    f.write("\n")

                print(
                    f"[{response.status_code}] {image_path.name} run={run_index} "
                    f"latency_ms={latency_ms:.1f}"
                )

    print("\nSummary")
    print(summarize_successes(results))
    print(f"\nSaved runs to: {output_path}")


if __name__ == "__main__":
    main()
