# Phase 2.1 baseline record — 2026-09-06

Ground-truth schema, annotation tooling and offline accuracy/calibration
calculations are implemented. The current labelled set is **0 Tier A, 2 Tier B,
1 Tier C**. Numeric nutrition accuracy remains unavailable: there are no
independently labelled whole-meal macros or edible gram weights in this set.
No invented labels were added to meet the suggested ten-case target.

The public Tier C case is `astons-all-day-breakfast-001`. Its mac-and-cheese
identity comes from the existing user correction dated 2026-09-05; its other
components, portions and macros remain unlabelled. The Tier B evidence is held
only in the existing ignored private manifest. The six attributed public source
photographs remain source-labelled, without numeric truth.

## Frozen collection

- Deployed application: `nutrition-estimator-v2`; model: `gpt-5.4`.
- Batch: `a91e4b5ef3b447d290d30aa211b58d06`.
- Three existing authorized images × three caption variants × two repeats:
  **18 completed estimate-only Lab calls**, no failed calls.
- Variants: no caption, generic caption, fact-rich caption. Tier C's fact-rich
  caption contains only its confirmed identity, not measured-portion information.
- Every estimate reports the pinned application/model. Source image bytes,
  labels, estimator configuration and user context remain fixed across variants.
- The synthetic user partition contains six records before and after; its hashes
  match. Logout was verified. No reset, correction, confirmation or cancellation
  was performed. Login/logout managed their normal session records.
- Existing estimate-only isolation tests continue to prove zero FitnessData
  writes by the estimate operation; before/after hashes additionally establish
  unchanged live domain state. No claim of a write-by-write cloud audit is made.

The report preserves per-run estimates and ranges, label snapshots, model/version,
latency, reconciliation, portion/identity checks, caption comparisons and separate
repeatability statistics. Macro MAE, median absolute error, MAPE, bias, numeric
portion error, and range coverage are null because their ground truth is absent.
Interval widths and follow-up behavior are observable without numeric truth.
Deterministic alias misses require review before being called food misidentification.
There is no supported ranking of nutritional-error causes from this dataset.

Local artifacts (all ignored, containing private-case data):

- `artifacts/nutrition/accuracy-baseline.json`: final machine-readable baseline.
- `artifacts/nutrition/private/accuracy-baseline.md`: detailed result/limitation report.
- `artifacts/nutrition/private/accuracy-collection-report.json`: original collection
  export, preserved before the final offline report added descriptive widths and
  matching observations. No images, labels, calls, or estimates were changed.
- `artifacts/nutrition/private/accuracy-live-verification.json`: independent
  post-run state and temporary-resource checks.
- `artifacts/nutrition/corpus.sqlite3`: append-only run history and frozen labels.

Collection records the starting Git HEAD (`7853be2`, including `90f2c0a`), the
estimator source hash and collection-tool hashes. The final report also records
its reporting-tool hashes. Artifact hashes are recorded in the private report.
The frozen final JSON SHA-256 is
`2f49404edd0a6f82dcfa9edd5b354b453e78c9e636a86282157c0b1802e7a2c6`.
Keep these files locally when reproducing the baseline; they are intentionally
not recoverable from Git. No private images, captions, labels, results or database
are committed.

## Validation and next step

The full offline suite passed **247 tests**. The separate Chromium Nutrition Lab
integration test passed. Runtime mirror checks, SAM template lint, JavaScript
syntax, Python compilation and `git diff --check` passed. No application or
infrastructure deployment was necessary. Estimator prompt, model, image detail,
dimensions, reconciliation, correction math, recommendations and priors were
not tuned or changed.

The next calibration goal must first supply defensible Tier A measured/official
nutrition labels for numeric accuracy comparisons, and review frozen alias
matching limitations. Preserve this initial baseline and collect a new labelled
batch for new evidence; do not rewrite historical labels or infer nutrition from
these predictions. Compare best-estimate errors and interval coverage/width
separately, with repeatability, outliers and source limitations visible.
