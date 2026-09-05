# Real-food regression corpus

This corpus supports **separate repeatability and ground-truth accuracy
evaluation**. Most photographs still lack weighed portions or complete macro
truth. Infrastructure support does not establish numeric accuracy: only
independently documented Tier A totals can do that. Previous model estimates
must never become ground truth. See the [ground-truth contract](GROUND_TRUTH_SCHEMA.md)
and [frozen baseline record](ACCURACY_BASELINE.md).

The versioned `manifest.json` and image files are the fixture registry. The local
SQLite database at `artifacts/nutrition/corpus.sqlite3` keeps cases, batches, and
individual results across test runs. It is outside the synthetic account reset
and the Lab's 24-hour result TTL. Back up that file if moving machines. The DB
and generated reports are ignored by Git; source fixtures and attribution are
versioned. No new backend deployment is needed for corpus changes.

## Initial coverage

| Case | Food | Evaluation dimensions |
| --- | --- | --- |
| `astons-all-day-breakfast-001` | Breakfast with **mac and cheese** | Multiple sides, eggs, sausage, grilled meat, sauce |
| `chicken-rice-001` | Hainanese chicken rice | Rice volume, chicken skin, sauce, egg |
| `masala-dosa-001` | Masala dosa | Hidden filling, chutneys, background food, warm lighting |
| `greek-salad-001` | Greek salad | Cheese, dressing/oil, vegetables, close crop |
| `chicken-biryani-001` | Chicken biryani | Rice, hidden oil, bone-in meat |
| `laksa-001` | Laksa | Noodles, broth, toppings, occlusion |
| `mac-and-cheese-001` | Mac and cheese | Cheese sauce, portion depth, low lighting |

These are seven **different real photographs**, not generated images. The six
new original files retain their source licenses and have not been modified;
see [ATTRIBUTION.md](ATTRIBUTION.md). Image hashes pin the exact uploaded bytes.
The existing breakfast photo's mac-and-cheese component is confirmed by the
user. Its label now says **mac and cheese**, replacing the generic pasta caption.
Older exported runs retain their actual historical caption; they are not rewritten
as though the corrected label was used at the time.

Source dish names and visual observations are not weighed ingredient or macro
labels. Unknown `known_portions_g`, `acceptable_macro_range`, and follow-up labels
remain empty/null. Do not score macro accuracy without defensible reference data.

## Use the database

```bash
# Validate images/hashes and sync the registry into SQLite; no model calls.
make nutrition-corpus

# Run two repeats per photo (14 real calls), only on the gated E2E account.
AWS_PROFILE=fitness-dev AWS_REGION=ap-southeast-1 \
  .venv/bin/python scripts/nutrition_variance.py --live --repeats 2 \
  --output artifacts/nutrition/variance-report.json

# Compare the same image with its labelled caption and no caption.
AWS_PROFILE=fitness-dev AWS_REGION=ap-southeast-1 \
  .venv/bin/python scripts/nutrition_variance.py --live --repeats 5 \
  --case astons-all-day-breakfast-001 --case mac-and-cheese-001 \
  --variants both --output artifacts/nutrition/caption-comparison.json

# Read the latest result or a prior batch; no model calls.
make nutrition-variance
.venv/bin/python scripts/nutrition_variance.py --report <batch-id>
```

`--case` is repeatable; omitting it selects the whole corpus. `--repeats` accepts
2–20 (default 3), `--variants` accepts `labelled`, `none`, or `both` (default
`labelled`). `--database` can point to another local SQLite file.
`scripts/nutrition_eval.py` and `make nutrition-eval` are compatibility entry
points for this same runner; their live mode now uses the gated browser Lab.

The live runner signs into `javaan-e2e`, uploads each photo through the actual
Nutrition Lab form, selects **estimate-only**, and waits for its structured job
result. It never clicks correction, confirm, or cancel. It validates the existing
dev stack and E2E marker/identity/credential mapping before login and compares
the entire synthetic user partition before and after, without exclusions.
Temporary jobs live in a separate dev-only table.
It does not reset the account. A domain change invalidates the batch's context.
Browser sessions stay in memory and are logged out on normal completion; no
credentials, cookies, traces, HARs, or login screenshots enter the database.

## What variance means here

For each fixed photo, exact caption variant, model, and estimator version, the
report shows:

- Calories, protein, carbs and fat: mean, min/max, **sample standard deviation**,
  and coefficient of variation (`100 × SD / mean`).
- Successful repeats, failures, end-to-end latency and reconciliation statuses.
- Follow-up-question frequency and normalized ingredient-name frequencies.
- Mean pairwise Jaccard similarity of item-name sets. This measures wording
  stability; synonyms can reduce the score without indicating a food error.

Caption variants, model versions, and image hashes are never pooled into one
variance statistic. The server's `usage.estimator_version` identifies the deployed
estimator; model-written version text is retained separately with frequency
counts because it can vary between otherwise equivalent runs. Each batch also records the Git HEAD, settings, creation
time, and a hash of the test user's prior domain context. Compare batches only
when those contexts are compatible. Each run retains its own case/label
snapshot, model/version, raw structured result, repeat number and latency.
Updating a fixture does not overwrite historical run data. A duplicate repeat
identifier cannot silently overwrite a result. Failures are recorded as error
categories without raw SDK messages and appear in separate unknown-model groups
when no result supplied the model/version.

One successful estimate is reported with no SD/CV, not zero variance; zero-mean
macros have no CV. Two repeats provide an initial spread, not a reliable
regression threshold. The model's reported low/high range describes meal
uncertainty and is separate from the observed spread between repeated calls.
The latter measures **repeatability, not nutritional accuracy**.

## Add or review a case

1. Add an actual JPEG, PNG, or WebP photograph under `images/` (up to 3 MB and
   25 megapixels). Keep recipe/portion variants as separate photos with distinct
   case ids; do not fabricate labelled variants by editing an image.
2. Add a unique manifest entry using the existing fields. Record dish category,
   useful variation tags, truthful caption, provenance, license and attribution.
3. Calculate `image_sha256` from the file bytes. The validator refuses altered
   bytes until the manifest hash is intentionally updated.
4. Preserve the distinction between user-confirmed component labels, source dish
   labels, visually observed components, and weighed or recipe-backed nutrition.
   Add annotation provenance when correcting a label.
5. Update attribution for external images, run `make nutrition-corpus`, then
   select the case for repeated live testing.

The durable-flow smoke test can also select a fixture:

```bash
JAVAAN_E2E_CASE=mac-and-cheese-001 make e2e-nutrition-lab
```

Unlike the variance runner, this smoke command resets/verifies the E2E baseline
before and after log/correction/confirm/cancel. Both browser tests read the default
breakfast label from the registry, so the mac-and-cheese correction stays in one
place.

Private photos explicitly authorized by the user can be imported with
`scripts/import_private_nutrition_photos.py`; see the private-fixture workflow in
[the E2E guide](../../docs/E2E_TESTING.md). The private combined manifest and
photos remain ignored under `artifacts/nutrition/private/`. Use `--manifest` to
include them. Original captions are preserved, and visual review tags distinguish
shared plates, packaging, product photos and screenshot/framing variants. These
references add realistic coverage but still do not establish macro accuracy.

## Enter defensible labels

- **Tier A:** weighed edible ingredients, weighed cooked portions with a known
  nutritional reference, package label with known consumed quantity, official
  restaurant nutrition with known consumed serving, or a weighed recipe with
  known consumed fraction. Complete meal macros and numeric error are supported.
- **Tier B:** independently known component facts, such as counts, edible grams,
  or whether skin was removed. Score only the dimensions actually supported.
- **Tier C:** confirmed identity/presence/absence. No portion or macro-error claims.

Source dish names, visual guesses, model-reported portions, old logged estimates,
other LLM answers, and generic nutrition database values without known consumed
quantity are not numeric truth. A user caption qualifies only for the explicit
known fact it contains. Do not infer equal consumption from “shared by two”.

```bash
make nutrition-label CASE=astons-all-day-breakfast-001
# Set EDITOR for the local JSON editor, or provide independent label JSON:
make nutrition-label CASE=<id> LABEL=artifacts/nutrition/private/label-input.json \
  NUTRITION_MANIFEST=artifacts/nutrition/private/manifest.json
make nutrition-groundtruth-check
make nutrition-groundtruth-check \
  NUTRITION_MANIFEST=artifacts/nutrition/private/manifest.json
```

The tool shows the image path and metadata. Supply `ground_truth` following the
contract and optionally `generic_caption`; the original caption is preserved.
It never fills missing facts. It validates image hash, provenance, tiers,
quantities and arithmetic, then writes only the selected manifest atomically.
Prior labels and provenance stay in `label_history`. The editor file is private
and temporary. Keep input files under ignored `artifacts/nutrition/private/`.
Never store credentials, session data, Telegram file IDs, or unrelated chat data.

For weighed meals, record **edible consumed** cooked grams and a matching
cooked-food reference with its source location. For packages, record the exact
product/label reference, per-100g or per-serving macros, and consumed grams or
known servings. For restaurants, record the official document, exact item and
customizations, and consumed serving/fraction. The dish name alone is insufficient.
For recipes, record weighed ingredients and the known consumed fraction, then
enter each ingredient's consumed share; fraction metadata is not applied twice.

The CLI derives nutrition only from supplied quantities and references,
persisting the arithmetic. It preserves official supplied totals and rejects
disagreement. Add no numeric label when evidence is unavailable. Review aliases
before freezing a baseline; ambiguous matches remain unmatched. Private fixture
manifests and images must stay in the ignored artifact area. The private manifest
cannot silently publish a fixture through the label tool.

## Run and freeze an accuracy baseline

```bash
# Read the existing baseline; no model calls or account reset:
make nutrition-accuracy

# Explicit selection/opt-in; prints planned paid calls before authentication:
AWS_PROFILE=fitness-dev AWS_REGION=ap-southeast-1 \
  .venv/bin/python scripts/nutrition_accuracy.py --live \
  --manifest artifacts/nutrition/private/manifest.json \
  --case <labelled-id> --repeats 2 \
  --variant none --variant generic --variant fact-rich \
  --output artifacts/nutrition/accuracy-baseline.json

# Rebuild offline from ORIGINAL immutable per-run label snapshots:
.venv/bin/python scripts/nutrition_accuracy.py --batch <batch-id> \
  --output artifacts/nutrition/accuracy-review.json
```

No Make target opts into live accuracy calls. Live accuracy requires `--case`;
repeats accept 1–20. One repeat has no SD/CV. Existing output files are refused
before model calls, protecting the frozen baseline. Generated reports/databases
are restricted to ignored `artifacts/nutrition/`. Label revisions affect future
batches, never historical snapshots.

Variants: `none` is empty; `generic` uses the explicit generic caption or falls
back to the original caption; `labelled` preserves the original caption;
`fact-rich` uses only annotated names, quantities and preparation facts, never
target macros. At Tier C it adds identity facts only, with no measured-portion
information. Inspect the exact saved strings. All variants must retain the same
image and ground-truth hash. Pairwise comparisons use shared case sets, with
models/application versions separate and failures visible.

Live accuracy uses the deployed browser Lab, only as `javaan-e2e`, and uploads
estimate-only jobs, then polls their exact IDs. It never selects correction,
confirm, cancel, or reset. The pinned baseline is `nutrition-estimator-v2 / gpt-5.4`;
a mismatch stops further calls and invalidates the batch. SQLite preserves raw
jobs, server version, latency, captions and label snapshots before Lab TTL expiry.
Reports include per-run actual/best/low/high, errors, coverage/width, portions,
identity, follow-ups and reconciliation. Existing SD, CV and item-name stability
remain explicitly separate from accuracy.

Whole synthetic user-partition hashes/counts are recorded before and after,
including failures, and logout is verified. Invalid contexts cannot support
accuracy claims. Login/logout manage normal browser session records; the
**estimate operation** has zero FitnessData writes, tested by the offline Lab
isolation suite. State hashes prove unchanged domain state, not a write-by-write
audit. No source-user priors are imported. No clean deployed prior toggle exists,
so enabled-versus-absent priors are deferred without changing production behavior.

For the next calibration goal, preserve the frozen image hashes, labels, captions,
context and versions. Add weighed Tier A cases before numeric accuracy claims.
Human-reviewed poor cases may carry failure categories with evidence; estimator
assumptions are not authoritative reviews. Optimize only in that next goal,
comparing case-balanced error **and** interval coverage/width while retaining
outliers and repeatability results.
