#!/usr/bin/env python3
"""Maintain the food corpus and run repeat estimates through the dev browser Lab."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.nutrition_corpus import (
    CorpusDatabase, DEFAULT_DATABASE, DEFAULT_MANIFEST, caption_for, digest, image_path, load_cases,
)


def wait_lab_job(request, base_url, job_id, *, timeout_seconds=210, sleep=time.sleep):
    """Poll the exact upload response id, independent of UI recent-job rendering."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = request.get(f"{base_url}/api/e2e/nutrition-lab/jobs/{job_id}", timeout=30_000)
        if response.status != 200:
            raise RuntimeError("Lab result request failed")
        job = response.json()
        if job.get("job_id") != job_id or job.get("mode") != "estimate":
            raise ValueError("Unexpected Lab result identity or mode")
        if job.get("status") == "complete":
            return job
        if job.get("status") == "failed":
            raise RuntimeError("Lab estimate failed")
        sleep(2)
    raise TimeoutError("Lab estimate did not finish before the deadline")


def open_ready_lab(page):
    page.get_by_test_id("bottom-navigation").wait_for(state="visible", timeout=30_000)
    page.locator("#app-shell").wait_for(state="visible", timeout=30_000)
    page.locator("#status-panel").wait_for(state="hidden", timeout=30_000)
    page.get_by_test_id("nav-nutrition").click()
    page.get_by_test_id("nutrition-lab").wait_for(state="visible", timeout=30_000)


def run_live(db, cases, *, manifest, repeats, variants):
    from playwright.sync_api import sync_playwright
    from scripts.e2e_support import dev_resources, load_e2e_credentials, read_e2e_records, validate_e2e_credential, user_partition_items
    from macro_bot.serverless_data import _from_storage

    session, table, outputs, repo = dev_resources()
    read_e2e_records(table)
    validate_e2e_credential(repo.get_web_credential("javaan-e2e"))
    username, password = load_e2e_credentials(session)

    def domain_snapshot():
        return sorted((_from_storage(item) for item in user_partition_items(table, "e2e-javaan-e2e")
                       if not item["SK"].startswith("LAB_JOB#")), key=lambda item: item["SK"])

    before = domain_snapshot()
    # Store the hash, not profile/auth data. It separates changed correction-prior contexts.
    context_hash = digest(before)
    git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=DEFAULT_MANIFEST.parents[2], text=True).strip()
    batch_id = db.start_batch(transport="dev_browser_lab", context_hash=context_hash,
        settings={"repeats": repeats, "variants": variants, "case_ids": [c["id"] for c in cases], "git_head": git_head})
    failed = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=os.getenv("JAVAAN_E2E_HEADLESS", "1") != "0")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                base_url = outputs["MiniAppUrl"].rstrip("/")
                page.goto(base_url + "/", wait_until="domcontentloaded", timeout=45_000)
                page.get_by_test_id("browser-login-username").fill(username)
                page.get_by_test_id("browser-login-password").fill(password)
                page.get_by_test_id("browser-login-submit").click()
                open_ready_lab(page)
                # No storageState, trace, HAR, video, cookies, or login screenshots are saved.
                for case in cases:
                    for variant in variants:
                        for repeat in range(1, repeats + 1):
                            started = time.monotonic()
                            try:
                                if digest(image_path(case, manifest).read_bytes()) != case["image_sha256"]:
                                    raise ValueError("Fixture changed during run")
                                page.get_by_test_id("lab-image").set_input_files(str(image_path(case, manifest)))
                                page.get_by_test_id("lab-caption").fill(caption_for(case, variant))
                                page.get_by_test_id("lab-mode").select_option("estimate")
                                with page.expect_response(lambda response:
                                    response.request.method == "PUT" and
                                    response.url.startswith(base_url + "/api/e2e/nutrition-lab/jobs/"),
                                    timeout=45_000) as submitted:
                                    page.get_by_test_id("lab-submit").click()
                                if submitted.value.status != 202:
                                    raise RuntimeError("Lab upload failed")
                                job_id = submitted.value.json()["job_id"]
                                job = wait_lab_job(context.request, base_url, job_id)
                                db.record(batch_id, case, variant, repeat, (time.monotonic()-started)*1000, job=job)
                                print(f"{case['id']} {variant} {repeat}/{repeats}: complete", flush=True)
                            except Exception as err:
                                failed = True
                                db.record(batch_id, case, variant, repeat, (time.monotonic()-started)*1000, error_category=type(err).__name__)
                                print(f"{case['id']} {variant} {repeat}/{repeats}: {type(err).__name__}", flush=True)
                                page.reload(wait_until="domcontentloaded", timeout=45_000)
                                open_ready_lab(page)
                page.get_by_test_id("logout").click()
                page.get_by_test_id("browser-login-form").wait_for(state="visible", timeout=30_000)
            finally:
                browser.close()
        if domain_snapshot() != before:
            db.finish(batch_id, "invalid_context_domain_changed")
            raise RuntimeError("Synthetic domain changed during the estimate-only batch; compare results only after reviewing context")
        db.finish(batch_id, "complete_with_errors" if failed else "complete")
    except BaseException:
        current = db.report(batch_id)["status"]
        if current == "running":
            db.finish(batch_id, "interrupted")
        raise
    return batch_id


def print_report(report):
    print(f"Batch: {report['batch_id'] or 'none'}")
    if not report["batch_id"]:
        return
    print(f"Status: {report['status']} · attempts: {report['attempts']}")
    for group in report["groups"]:
        kcal = group["macros"]["calories"]
        print(f"{group['case_id']} / {group['variant']}: n={group['successes']} "
              f"kcal={kcal['min']}–{kcal['max']} SD={kcal['sample_stddev']} CV={kcal['cv_pct']}% "
              f"failures={group['failures']}")
    print(report["interpretation"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--case", action="append", dest="case_ids", help="select a case id; repeatable")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--variants", choices=("labelled", "none", "both"), default="labelled")
    parser.add_argument("--live", action="store_true", help="make real estimate-only model calls via the gated browser Lab")
    parser.add_argument("--report", nargs="?", const="latest", help="show latest or specified historical batch")
    parser.add_argument("--output", type=Path, help="export a structured report to JSON")
    args = parser.parse_args(argv)
    if not 2 <= args.repeats <= 20:
        parser.error("--repeats must be between 2 and 20")
    if args.live and args.report:
        parser.error("choose --live or --report")
    cases = load_cases(args.manifest)
    if args.case_ids:
        unknown = set(args.case_ids) - {c["id"] for c in cases}
        if unknown: parser.error("Unknown case ids: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case["id"] in args.case_ids]
    variants = ["labelled", "none"] if args.variants == "both" else [args.variants]
    db = CorpusDatabase(args.database)
    try:
        db.sync_cases(cases)
        if args.live:
            batch_id = run_live(db, cases, manifest=args.manifest, repeats=args.repeats, variants=variants)
            report = db.report(batch_id)
        elif args.report:
            report = db.report(None if args.report == "latest" else args.report)
        else:
            print(f"Registered {len(cases)} real image cases in {db.path}")
            for case in cases: print(f"{case['id']}: {case['food_type']}")
            print(f"Planned: {len(cases) * len(variants) * args.repeats} estimate-only calls; use --live to run")
            return 0
        print_report(report)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
        return 0 if report.get("status") == "complete" else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
