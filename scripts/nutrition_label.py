#!/usr/bin/env python3
"""Enter independent labels into exactly one selected corpus manifest."""
from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.nutrition_corpus import DEFAULT_MANIFEST, ROOT, digest, image_path, load_cases, private_case
from scripts.nutrition_groundtruth import prepare_ground_truth, require, require_private_output, validate_ground_truth, write_json


def apply_label(manifest, case_id, payload):
    """Only this entry point writes labels. Model jobs/raw results are not inputs."""
    manifest = Path(manifest).resolve()
    require(set(payload) <= {"ground_truth", "generic_caption"} and "ground_truth" in payload,
            "Input must contain independent ground_truth, never a model result")
    original = manifest.read_bytes()
    cases = load_cases(manifest)
    require(case_id in {c["id"] for c in cases}, "Unknown case id")
    case = next(c for c in cases if c["id"] == case_id)
    if private_case(case):
        require_private_output(manifest)
    prior = {k: copy.deepcopy(case[k]) for k in ("ground_truth", "generic_caption", "label_status") if k in case}
    case["ground_truth"] = prepare_ground_truth(payload["ground_truth"])
    case["label_status"] = "ground_truth"
    if "generic_caption" in payload:
        case["generic_caption"] = payload["generic_caption"]
    validate_ground_truth(case)
    # Preserve prior labels, including their provenance, in the same manifest.
    if prior != {k: case[k] for k in prior}:
        case.setdefault("label_history", []).append({"previous": prior, "replaced_by": case["ground_truth"]["provenance"]})
    require(manifest.read_bytes() == original, "Manifest changed during edit; reload before retrying")
    require(digest(image_path(case, manifest).read_bytes()) == case["image_sha256"], "Image changed during annotation")
    write_json(manifest, cases)
    return case


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--case")
    parser.add_argument("--input", type=Path, help="Independent label JSON; no estimator results")
    parser.add_argument("--check", action="store_true", help="Offline label/image validation only")
    args = parser.parse_args(argv)
    cases = load_cases(args.manifest)
    if args.check:
        print(json.dumps({t: sum(c.get("ground_truth", {}).get("confidence_tier") == t for c in cases) for t in ("A", "B", "C")}))
        print(f"Validated {len(cases)} images and all supplied labels; no model calls.")
        return 0
    if not args.case:
        print("Cases: " + ", ".join(c["id"] for c in cases))
        print("Choose --case (make nutrition-label CASE=<id>) and --manifest for private labels.")
        return 0
    case = next((c for c in cases if c["id"] == args.case), None)
    require(case is not None, "Unknown case id")
    print(f"Image: {image_path(case, args.manifest)}")
    print(json.dumps(case, indent=2, ensure_ascii=False))
    if args.input:
        payload = json.loads(args.input.read_text())
    else:
        editor = os.getenv("EDITOR")
        require(editor, "Set EDITOR or supply --input label.json; no missing facts will be inferred")
        directory = ROOT / "artifacts/nutrition/private"
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(suffix=".json", dir=directory)
        os.close(fd)
        path = Path(name)
        try:
            template = {"ground_truth": case.get("ground_truth", {
                "schema_version": 1, "confidence_tier": "", "source_type": "", "values_kind": "",
                "provenance": {"date": "", "source": "", "method": "", "reference": ""},
                "components_complete": False, "components": []})}
            write_json(path, template)
            subprocess.run([*shlex.split(editor), str(path)], check=True)
            payload = json.loads(path.read_text())
        finally:
            path.unlink(missing_ok=True)
    updated = apply_label(args.manifest, args.case, payload)
    print(json.dumps(updated["ground_truth"], indent=2, ensure_ascii=False))
    print(f"Validated label saved only to {args.manifest}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
