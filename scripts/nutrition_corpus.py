"""Versioned real-food fixtures and append-only local evaluation history."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "evals/nutrition/manifest.json"
DEFAULT_DATABASE = ROOT / "artifacts/nutrition/corpus.sqlite3"
MACROS = ("calories", "protein_g", "carbs_g", "fat_g")


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def load_cases(manifest=DEFAULT_MANIFEST):
    from macro_bot.nutrition_lab import validate_image
    manifest = Path(manifest).resolve()
    cases = json.loads(manifest.read_text())
    if not isinstance(cases, list) or not cases:
        raise ValueError("Nutrition manifest must contain cases")
    ids = set()
    for case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError("Every corpus case needs a unique id")
        ids.add(case_id)
        path = (manifest.parent / case["image"]).resolve()
        raw = path.read_bytes()
        validate_image(raw)
        if digest(raw) != case.get("image_sha256"):
            raise ValueError(f"Image checksum changed: {case_id}")
        if not case.get("food_type") or not case.get("source") or not isinstance(case.get("caption"), str):
            raise ValueError(f"Missing fixture metadata: {case_id}")
        if len(case["caption"]) > 1000:
            raise ValueError(f"Caption too long: {case_id}")
        from scripts.nutrition_groundtruth import validate_ground_truth, require_private_output
        if private_case(case) or path.is_relative_to(ROOT / "artifacts/nutrition/private"):
            require_private_output(manifest)
            require_private_output(path)
        validate_ground_truth(case)
    return cases


def image_path(case, manifest=DEFAULT_MANIFEST):
    return (Path(manifest).resolve().parent / case["image"]).resolve()


def private_case(case):
    source = case.get("source", {})
    return source.get("redistribution") == "private_local_only" or source.get("kind") == "authorized_private_telegram_submission"


def case_by_id(case_id, manifest=DEFAULT_MANIFEST):
    return next(case for case in load_cases(manifest) if case["id"] == case_id)


def caption_for(case, variant):
    if variant == "none":
        return ""
    if variant == "labelled":
        return case["caption"]
    if variant == "generic":
        return case.get("generic_caption", case["caption"])
    if variant == "fact-rich":
        from scripts.nutrition_groundtruth import fact_rich_caption
        return fact_rich_caption(case)
    raise ValueError("Unknown caption variant")


def estimator_version(job):
    # The estimate's version text is model-generated. Server usage metadata is
    # authoritative; retain model text in raw results and report its variation.
    return (job.get("usage") or {}).get("estimator_version") or (job.get("estimate") or {}).get("estimator_version")


class CorpusDatabase:
    def __init__(self, path=DEFAULT_DATABASE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.path.chmod(0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY, image_sha256 TEXT NOT NULL,
                food_type TEXT NOT NULL, metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                transport TEXT NOT NULL, context_hash TEXT NOT NULL,
                settings_json TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(batch_id),
                case_id TEXT NOT NULL REFERENCES cases(case_id),
                image_sha256 TEXT NOT NULL, variant TEXT NOT NULL, caption TEXT NOT NULL,
                repeat_index INTEGER NOT NULL, case_snapshot_json TEXT NOT NULL,
                model TEXT, estimator_version TEXT, latency_ms REAL NOT NULL,
                result_json TEXT, error_category TEXT, created_at TEXT NOT NULL,
                UNIQUE(batch_id, case_id, variant, repeat_index)
            );
        """)

    def close(self):
        self.connection.close()

    def sync_cases(self, cases):
        with self.connection:
            for case in cases:
                if private_case(case):
                    from scripts.nutrition_groundtruth import require_private_output
                    require_private_output(self.path)
                self.connection.execute("""INSERT INTO cases VALUES (?, ?, ?, ?)
                    ON CONFLICT(case_id) DO UPDATE SET image_sha256=excluded.image_sha256,
                    food_type=excluded.food_type, metadata_json=excluded.metadata_json""",
                    (case["id"], case["image_sha256"], case["food_type"], json.dumps(case, ensure_ascii=False)))

    def start_batch(self, *, transport, context_hash, settings):
        batch_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute("INSERT INTO batches VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, datetime.now(timezone.utc).isoformat(), transport, context_hash, json.dumps(settings), "running"))
        return batch_id

    def record(self, batch_id, case, variant, repeat_index, latency_ms, *, job=None, error_category=None):
        if private_case(case):
            from scripts.nutrition_groundtruth import require_private_output
            require_private_output(self.path)
        estimate = job.get("estimate") if job else None
        if job is not None:
            if job.get("mode") != "estimate" or "action" in job or job.get("status") != "complete":
                raise ValueError("Only completed estimate-only jobs belong in the variance corpus")
            if not isinstance(estimate, dict):
                raise ValueError("Missing structured estimate")
            for metric in MACROS:
                value = estimate.get("total_best", {}).get(metric)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError("Invalid macro value in Lab result")
        if (job is None) == (error_category is None):
            raise ValueError("Record either a completed estimate or an error")
        with self.connection:
            self.connection.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                uuid.uuid4().hex, batch_id, case["id"], case["image_sha256"], variant,
                caption_for(case, variant), repeat_index, json.dumps(case, ensure_ascii=False),
                job.get("model") if job else None, estimator_version(job) if job else None,
                latency_ms, json.dumps(job, ensure_ascii=False) if job else None, error_category,
                datetime.now(timezone.utc).isoformat(),
            ))

    def finish(self, batch_id, status="complete"):
        with self.connection:
            self.connection.execute("UPDATE batches SET status=? WHERE batch_id=?", (status, batch_id))

    def update_settings(self, batch_id, evidence):
        row = self.connection.execute("SELECT settings_json FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        settings = {**json.loads(row[0]), **evidence}
        with self.connection:
            self.connection.execute("UPDATE batches SET settings_json=? WHERE batch_id=?", (json.dumps(settings), batch_id))

    def report(self, batch_id=None):
        if batch_id is None:
            row = self.connection.execute("SELECT batch_id FROM batches ORDER BY created_at DESC LIMIT 1").fetchone()
            if row is None:
                return {"batch_id": None, "groups": [], "message": "No evaluation runs yet"}
            batch_id = row["batch_id"]
        batch = self.connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None:
            raise ValueError("Unknown evaluation batch")
        rows = self.connection.execute("SELECT * FROM runs WHERE batch_id=? ORDER BY case_id, variant, repeat_index", (batch_id,)).fetchall()
        return {"batch_id": batch_id, "created_at": batch["created_at"], "status": batch["status"],
                "transport": batch["transport"], "context_hash": batch["context_hash"],
                "settings": json.loads(batch["settings_json"]), "attempts": len(rows),
                "groups": summarize_runs([dict(row) for row in rows]),
                "interpretation": "Sample variation between repeat estimates, not accuracy against weighed food. Caption variants and model versions are separate groups."}


def sample_stats(values):
    if not values:
        return {"n": 0, "mean": None, "min": None, "max": None, "sample_stddev": None, "cv_pct": None}
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if len(values) > 1 else None
    return {"n": len(values), "mean": round(mean, 3), "min": min(values), "max": max(values),
            "sample_stddev": round(sd, 3) if sd is not None else None,
            "cv_pct": round(100 * sd / abs(mean), 3) if sd is not None and mean != 0 else None}


def summarize_runs(rows):
    groups = defaultdict(list)
    for row in rows:
        # Never mix changed labels, image bytes, models, or application versions.
        job = json.loads(row["result_json"]) if row["result_json"] else None
        version = estimator_version(job) if job else row["estimator_version"]
        key = (row["case_id"], row["image_sha256"], row["variant"], row["caption"], row["model"], version)
        groups[key].append(row)
    output = []
    for key, entries in groups.items():
        jobs = [json.loads(row["result_json"]) for row in entries if row["result_json"]]
        estimates = [job["estimate"] for job in jobs]
        item_sets = [set(" ".join(item["name"].casefold().split()) for item in estimate.get("items", [])) for estimate in estimates]
        # Exact normalized names intentionally remain visible: synonym changes are
        # measured as label churn, not silently equated with food identity errors.
        pairs = [len(a & b) / len(a | b) if a | b else 1.0
                 for index, a in enumerate(item_sets) for b in item_sets[index + 1:]]
        case = json.loads(entries[0]["case_snapshot_json"])
        output.append({"case_id": key[0], "food_type": case["food_type"], "image_sha256": key[1],
            "variant": key[2], "caption": key[3], "model": key[4], "estimator_version": key[5],
            "attempts": len(entries), "successes": len(jobs), "failures": len(entries)-len(jobs),
            "macros": {metric: sample_stats([estimate["total_best"][metric] for estimate in estimates]) for metric in MACROS},
            "latency_ms": sample_stats([row["latency_ms"] for row in entries]),
            "reconciliation_counts": dict(Counter(e.get("reconciliation_status", "unknown") for e in estimates)),
            "reported_estimator_version_counts": dict(Counter(e.get("estimator_version", "unknown") for e in estimates)),
            "follow_up_count": sum(bool(e.get("follow_up_question")) for e in estimates),
            "item_name_frequency": dict(Counter(name for names in item_sets for name in names)),
            "mean_pairwise_item_name_jaccard": round(statistics.mean(pairs), 3) if pairs else None,
            "sample_note": "At least 2 successful repeats needed" if len(jobs)<2 else "Small sample; increase repeats before setting regression thresholds",
        })
    return output
