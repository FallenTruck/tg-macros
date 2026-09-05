# JavaanFitness live E2E testing

This repository has a live Chromium harness for the deployed development Mini
App. It is intended for frontend and end-to-end work that needs real browser
navigation, browser-session authentication, responsive checks, and screenshots.
It is not part of the normal offline test suite.

## Safety boundary

The harness uses only the dedicated synthetic account `javaan-e2e` in the
`tg-macros-dev` stack in `ap-southeast-1`, through the `fitness-dev` AWS
profile. It does not use Vaan's or Pooja's account. The account has:

- an `account_type=e2e` marker at `E2E_ACCOUNT#javaan-e2e`
- a canonical identity in the separate `IDENTITY#E2E#javaan-e2e` namespace
- internal user id `e2e-javaan-e2e`
- no actual Telegram account and no elevated application privileges

The synthetic identity uses Telegram id `0` only as an explicit non-Telegram
sentinel. Telegram init data cannot produce that id, and the normal Telegram
identity resolver never creates it.

Do not change the account marker, identity, credential mapping, or reset script
to target a normal user. The reset command validates all of them before it
deletes anything, then deletes only the exact `USER#e2e-javaan-e2e` partition.
Shared programme records, other users, and normal identity partitions are not
scanned or modified.

## Install the E2E dependency

Activate the repository environment, then install the E2E-only dependency and
Chromium:

```bash
source .venv/bin/activate
python -m pip install -r requirements-e2e.txt
python -m playwright install chromium
```

Playwright is not in the Lambda requirements and E2E tooling does not require
Docker Desktop. This Mac uses Colima only for SAM builds.

## Provision or rotate the account

After reviewing the source and validating the offline tests, run:

```bash
make e2e-provision
```

The command discovers `FitnessDataTableName` from the `tg-macros-dev` stack,
creates or reuses the marked synthetic identity, generates a cryptographically
random password, stores the username in:

```text
/tg-macros/dev/e2e/web_username
```

and stores the password as an SSM `SecureString` in:

```text
/tg-macros/dev/e2e/web_password
```

The password is held only in process memory while the command hashes and
stores it. It is never printed, accepted as a command-line argument, written
to disk, or placed in Git. The DynamoDB credential contains only the normal
versioned password-hash representation plus its synthetic identity mapping.

The direct supported rotation command is:

```bash
AWS_PROFILE=fitness-dev AWS_REGION=ap-southeast-1 \
  .venv/bin/python scripts/provision_e2e_account.py --replace
```

Rotation is the only operation that replaces the SSM password or browser hash.
Do not copy the generated value into a shell history, issue, screenshot, trace,
video, HAR, report, or chat.

## Reset the deterministic baseline

Run the interactive reset before a manual scenario:

```bash
make e2e-reset
```

The prompt requires typing `javaan-e2e`. The reset verifies the E2E marker,
synthetic identity, reserved user id, and browser credential mapping before
deleting user-owned profile, target, nutrition, workflow, workout, and history
records. It then writes the deterministic baseline profile:

```text
male · age 30 · 180 cm · 80 kg · moderately active · maintain · Asia/Singapore
```

It also adds one confirmed `E2E baseline meal` for the current Singapore local
day so the live nutrition history check exercises a non-empty meal list.

The live smoke targets only this account and may be reset again afterward.

## Run live checks

The live tests obtain the Mini App URL from CloudFormation and fetch both SSM
parameters at runtime. They use one in-memory Playwright browser context per
run; no `storageState` file, trace, video, or HAR is enabled.

```bash
make e2e-smoke
```

The smoke test covers browser login, generic bad-password rejection, refresh
session persistence, logout, Home/Nutrition/Profile/Workout navigation, local-day
nutrition history navigation, responsive overflow checks at 390 px, 360 px, and
desktop width, and a PULL workout that:

1. saves a working set
2. repeats the previous set and saves it
3. skips a set
4. skips exercises until submission is ready
5. verifies the sticky completion state
6. submits the workout and verifies the success state

The runner can be watched with `JAVAAN_E2E_HEADLESS=0` while retaining the same
secret-handling rules.

## Capture screenshots

```bash
make e2e-screenshots
```

Screenshots are written to the ignored `artifacts/e2e/` directory:

- `home-mobile.png`
- `nutrition-mobile.png`
- `workout-programme-mobile.png`
- `workout-active-mobile.png`
- `workout-complete-mobile.png`
- `profile-desktop.png`

Screenshots are taken only after authentication; the password field is never
captured while populated. Generated artifacts are not committed by default.

## Future Codex workflow

For frontend or end-to-end work, use this order:

1. run unit and integration tests
2. deploy only when the change requires it
3. verify the stack and CloudFront release
4. run `make e2e-reset`
5. run `make e2e-smoke`
6. run `make e2e-screenshots` for changed UI and inspect the files
7. report concrete screenshot paths and observed results

Never run these commands against a non-development stack. If Chromium or
Playwright installation fails, report the exact installation error instead of
performing broad machine troubleshooting.

## E2E Nutrition Lab

The hidden `#nutrition-lab` route exposes **E2E Nutrition Lab** only to the validated
`javaan-e2e` browser session. Open the deployed Mini App URL with `#nutrition-lab`
after login (direct links also survive login). It is absent from the four-item
bottom navigation and normal Nutrition history. Unauthorized sessions redirect
to Home. This is an evaluation and regression surface.
Normal browser users, Telegram launch sessions, and unauthenticated callers
cannot access any Lab endpoint. Hiding the controls is not the security boundary:
every endpoint and asynchronous worker validates the canonical E2E marker,
identity, credential mapping, reserved user partition, and deployment gate.

Deployment requires `EnableE2ENutritionLab=true`, `EnvironmentName=dev`, the exact
`tg-macros-dev` stack, and `ap-southeast-1`. The parameter defaults to false;
`samconfig.toml` opts in only the documented development stack. The dedicated
worker has DynamoDB write permission only for `USER#e2e-javaan-e2e` and has no
Telegram Bot token permission. Disable with `EnableE2ENutritionLab=false` through
SAM. Never enable this surface for normal accounts or another environment.

Use a real JPEG, PNG, or WebP image, up to 3 MB and 25 megapixels, with an optional
caption of at most 1,000 characters. Images pass unchanged to the shared service,
then the existing DirectOpenAIEstimator preprocessing, strict OpenAI schema,
validation, and reconciliation. The browser does not infer nutrition or resize
images. The smaller upload cap leaves room for JSON/base64 in the API invocation.

- **Estimate-only:** shows structured estimate, item evidence, uncertainty,
  reconciliation, model and usage. It creates a temporary result only in the separate dev-only `NutritionLabJobs`
  table (`LAB#javaan-e2e` partition). It makes **zero writes to FitnessDataTable**,
  including the entire E2E user partition; no meal, action, correction, workflow,
  recommendation or daily-history record is created.
- **Full synthetic log:** creates the production pending meal/action/detail
  records, exposes the production contextual corrections, and allows confirm or
  cancel. Confirmation updates the daily read model and starts the same
  recommendation planner/client/fallback path used by Telegram. Recommendation
  failure does not undo a confirmed meal. The existing one-hour pending deadline
  and scheduled auto-confirm semantics apply; a browser refresh does not cancel
  the meal. The worker never sends a Telegram message for this account.

The Lab uses an asynchronous Lambda job so an estimate does not race the HTTP
API timeout. The private encrypted `NutritionLabImages` bucket holds uploaded
bytes only during processing; the worker deletes the object on success or
failure, with a one-day S3 lifecycle fallback for interruption/orphan uploads.
Jobs and structured evaluation results in the separate ephemeral table have a
24-hour DynamoDB TTL and are
hidden after expiry. Durable meals and correction feedback retain the production
retention rules. Neither raw image bytes nor credentials enter DynamoDB jobs,
logs, screenshots, or result downloads. The user-selected photo can appear in
post-authentication screenshots. Exported result JSON contains the caption and
nutrition result, so keep exports local unless intentionally sharing test data.

The browser lists recent jobs and resumes polling after reload. Repeating the
same upload request id and payload returns the same job; different payloads with
that id are rejected. Duplicate worker delivery is conditionally claimed once.
After a worker interruption, polling terminates after three minutes and exposes
any already durable action. Run a new job to retry estimation. Corrections are
never automatically replayed after a lost HTTP response; inspect the refreshed
result before applying another correction. Confirm/cancel retain production
idempotency. The reset command restores only the exact test user partition and rotates an
E2E profile reset revision. Queued/in-flight analysis checks that revision before
creating a pending meal and rejects a changed revision. Run reset and full-log
scenarios sequentially; do not reset while another tester is submitting meals.
Ephemeral jobs and images expire independently; legacy Lab jobs in the user
partition are removed by the existing reset. The persistent local corpus database
is independent of both reset and TTL.

API routes (all require the gated browser session; writes also require the
configured same-origin check):

- `GET /api/e2e/nutrition-lab/jobs` — recent runs
- `PUT /api/e2e/nutrition-lab/jobs/{32-hex-request-id}` — JSON with `image_base64`,
  `caption`, and `mode` (`estimate` or `log`), returning 202. Full logs may also
  provide `eaten_at`: an ISO instant or a local date/time interpreted in the saved
  profile timezone. No client timezone field is accepted. Past local dates
  suppress current-day recommendations after confirmation
- `GET /api/e2e/nutrition-lab/jobs/{id}` — structured status/result and current action
- `POST /api/e2e/nutrition-lab/jobs/{id}/correct` — `type` and `value` from the
  returned correction choices
- `POST /api/e2e/nutrition-lab/jobs/{id}/confirm` or `/cancel` — empty JSON body

Client-supplied user ids, action tokens, estimator/model overrides, image URLs,
and partition keys are not accepted. Lab routes reject query parameters.

Machine-readable results contain the complete `MealEstimate`, application-stamped
`estimator_version`, model/usage, reconciliation, follow-up, real `latency_ms`,
and the production `telegram_preview`. A pending action retains its original
estimate and exposes the current corrected estimate. Confirmed responses include
`daily_state`, recommendation status/payload and the production recommendation
preview (or a clear fallback). Model-provided version strings are unconditionally
overwritten in the common estimator validator. The UI displays totals/ranges,
items, assumptions/confidence, previews and downloadable JSON.

The Adjust button reveals production-provided base, skin, sauce/oil and whole
portion controls when applicable. Corrections update the result and preview.
Recommendation dispatch or generation failure never rolls back confirmation.

Run offline boundary and browser tests:

```bash
.venv/bin/python -m unittest tests.test_nutrition_lab
.venv/bin/python -m unittest e2e.test_nutrition_lab_browser.NutritionLabBrowserTests
```

The offline browser test uploads the real repository image while stubbing cloud
services and the OpenAI response. It proves browser integration, not model
accuracy. The explicit live test resets and verifies the synthetic baseline before and
in cleanup after the scenario, even on assertion failure. It makes three
real image/model calls for estimate-only, corrected confirmation/recommendation,
and cancellation:

```bash
make e2e-nutrition-lab
```

Override the photograph with `JAVAAN_E2E_MEAL_IMAGE=/absolute/path/meal.jpg` and
optionally set `JAVAAN_E2E_MEAL_CAPTION`. A custom image defaults to no caption.
The default is `images/6143401176322477320.jpg`. The live test verifies zero
estimate-only domain writes, durable refresh, corrected daily macros,
recommendation completion, cancellation without consumption changes, responsive
layout, and logged-out denial. It verifies the exact application version, model, latency,
production preview and an unchanged entire user partition for estimate-only. It produces `nutrition-lab-estimate-mobile.png`,
`nutrition-lab-confirmed-mobile.png`, and `nutrition-lab-desktop.png` in ignored
`artifacts/e2e/`, alongside `nutrition-lab-live-results.json` containing the
three structured run results. Tests assert pipeline behavior and structure, not nutritional
accuracy without human-reviewed labels.

### Persistent food corpus and variance runs

The real-image registry now lives in `evals/nutrition/manifest.json`, with a
persistent local SQLite history at `artifacts/nutrition/corpus.sqlite3`. Run
`make nutrition-corpus` to validate/sync it and `make nutrition-variance` to read
the latest report. For repeated real browser estimates use
`scripts/nutrition_variance.py --live --repeats 3`; it selects only estimate-only
mode, preserves domain records, and saves results before Lab job TTL expiry.
`--variants both` compares fixed labelled captions against no caption, in
separate statistical groups. See [the corpus guide](../evals/nutrition/README.md)
for the seven initial foods, image attribution, label rules, and historical
results. `JAVAAN_E2E_CASE` selects a registry fixture for the existing full-flow
smoke. The default breakfast caption now uses the user-confirmed **mac and
cheese** label.

### Authorized private Telegram fixtures

Use retained Telegram meal references only when the user explicitly authorizes
specific accounts. For the authorized Pooja/Vaanavan corpus:

```bash
AWS_PROFILE=fitness-dev AWS_REGION=ap-southeast-1 \
  .venv/bin/python scripts/import_private_nutrition_photos.py \
  --account pooja --account vaanavan --limit 4
.venv/bin/python scripts/nutrition_variance.py \
  --manifest artifacts/nutrition/private/manifest.json
```

The importer reads only identities and the selected accounts' retained meal
references, uses Telegram file download APIs, and writes no real-user records or
Telegram messages. It does not read unrelated chat history or poll bot updates.
Photos, original captions, reference hashes and private manifests remain under
ignored `artifacts/nutrition/private/`. No bot token, session state or raw file ID
is stored in the manifest. The combined private manifest can be selected with
`--manifest` for repeated Lab tests; `--case` selects individual cases. All model
calls and synthetic logs still run as `javaan-e2e`, never as the source account.
Review each imported image and tag shared plates, occluded food, screenshots,
product photos and alternate framing appropriately. Source captions and visual
observations are not weighed nutrition labels. Do not publish private fixtures.

### Ground-truth accuracy baseline (Phase 2.1)

Use `make nutrition-groundtruth-check` for offline validation and
`make nutrition-label CASE=<id>` for independent annotation. Select private data
with `NUTRITION_MANIFEST=artifacts/nutrition/private/manifest.json`. See the
[label contract and workflow](../evals/nutrition/README.md#enter-defensible-labels).

`make nutrition-accuracy` only reads the saved report. Real calls require
`scripts/nutrition_accuracy.py --live --case <id>`; case and variant selection is
repeatable and repeats are bounded. Planned calls are printed before login.
It reuses the deployed Lab browser upload/poll path in estimate-only mode.
No deployment is needed for this tooling-only phase. Prompt, model, image
preprocessing, correction math, priors and reconciliation remain unchanged.

The runner pins `nutrition-estimator-v2 / gpt-5.4`, keeps per-run label snapshots,
verifies logout even after failure, and records before/after hashes and counts
for the entire synthetic user partition. It never resets the baseline. Invalid
version/context batches cannot support accuracy reports. Offline isolation tests
cover zero estimate writes; browser login/logout still manage session records.
S3 deletion, job TTL, authentication gates and full-log flows remain unchanged.

Reports separate Tier A macro errors, measured-gram portion errors, deterministic
identity matching, range coverage/width, caption effects and repeatability.
Tier B counts and Tier C identities never become macro truth. Private reports
remain ignored. See the [baseline record](../evals/nutrition/ACCURACY_BASELINE.md)
for results, unavailable dimensions, and constraints on the next calibration goal.

### Phase 2.3 recommendation scenarios

After a backend deployment, run `make e2e-recommendations`. The script validates
only the marked `javaan-e2e` identity/credential, resets that exact baseline,
and invokes the deployed `NutritionLabFunction` with the fixed internal
`recommendation_scenarios` operation. This is an IAM-only dev smoke entry point,
not an HTTP endpoint: it accepts no clock, user ID, macros, or Telegram destination.
The Lambda rechecks all existing Lab gates and identity records.

It reads the persisted synthetic profile/confirmed meals and uses 18:30 and 22:30
in that profile's timezone to exercise two real recommendation calls. Assertions
check that a full meal ranks normally early, a light/protein option leads late,
all suggestions use supplied IDs, and the entire synthetic user partition is
unchanged during scenarios. The script restores the baseline in cleanup and
writes `artifacts/e2e/recommendation-scenarios.json`. No real user is queried,
modified or messaged; no photo estimation/accuracy benchmark runs are required.
Run this sequentially with all other E2E resets and logging tests.

`make recommendation-benchmark` is the offline counterpart, including all ten
requested nutrition scenarios, timezone/boundary tests, catalogue restriction
filtering, candidate-ID validation, Telegram formatting, delivery ordering,
historical suppression, duplicate callbacks and failure isolation. The Lab's
confirmed JSON now includes `nutrition_state_telegram_preview` separately from
`recommendation_telegram_preview`; existing estimate previews remain unchanged.


### Nutrition Profile V2 and retrospective logging

Run `make profile-browser` offline before deployment. It exercises the actual
Profile form and API through an isolated fake repository: both allowance choices,
food/style preferences, a forbidden ingredient, bedtime, list validation,
save/reload and target recalculation without wiping settings.

After the full backend/frontend release and CloudFront invalidation, run
`make e2e-profile` and then `make e2e-retrospective`, sequentially. The Profile
runner resets the marked account before and after, verifies logout, and writes
`artifacts/e2e/profile-live/profile-v2-mobile.png` and `profile-v2-desktop.png`.
No credentials/cookies are captured or persisted.

The retrospective runner calls the existing dev Lambda's separate IAM-only
`retrospective_scenario` operation with one of five fixed scenario names. It
accepts no user ID, timestamp, macro inputs or message destination. The Lambda
revalidates the complete synthetic identity and uses production selected-datetime,
pending-meal and confirmation operations with fixed synthetic macros. It makes
four recommendation model calls; the historical case must skip ranking. It does
not call the photo estimator or the Lab evaluation job path.

Fixtures use a calendar date two days ahead with an injected current clock,
avoiding the reset's current-day baseline meal in the historical case and
preventing background expiry sweeps from treating fixture actions as expired.
Earlier meals are confirmed at their actual time, then breakfast is entered at
22:00. The previous-day case owns only its historical day. Resets occur before
each case and in final cleanup, deleting only the validated synthetic partition.
The account must not be used concurrently by another test. The report is
`artifacts/e2e/retrospective-scenarios.json`. Assertions cover chronology, totals,
confirmation metadata, qualified wording, moderate/light choices and a 22:45
bedtime. No Telegram messages are sent.
