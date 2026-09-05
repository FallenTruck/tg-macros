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

The Nutrition tab exposes **E2E Nutrition Lab** only to the validated
`javaan-e2e` browser session. This is an evaluation and regression surface.
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
  reconciliation, model and usage. It creates only a temporary `LAB_JOB#` result;
  it never creates a meal, action, correction, or daily nutrition change.
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
Jobs and structured evaluation results have a 24-hour DynamoDB TTL and are
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
idempotency. The reset command already deletes Lab jobs in the exact test user
partition; temporary image objects expire independently.

API routes (all require the gated browser session; writes also require the
configured same-origin check):

- `GET /api/e2e/nutrition-lab/jobs` — recent runs
- `PUT /api/e2e/nutrition-lab/jobs/{32-hex-request-id}` — JSON with `image_base64`,
  `caption`, and `mode` (`estimate` or `log`), returning 202
- `GET /api/e2e/nutrition-lab/jobs/{id}` — structured status/result and current action
- `POST /api/e2e/nutrition-lab/jobs/{id}/correct` — `type` and `value` from the
  returned correction choices
- `POST /api/e2e/nutrition-lab/jobs/{id}/confirm` or `/cancel` — empty JSON body

Client-supplied user ids, action tokens, estimator/model overrides, image URLs,
and partition keys are not accepted.

Run offline boundary and browser tests:

```bash
.venv/bin/python -m unittest tests.test_nutrition_lab
.venv/bin/python -m unittest e2e.test_nutrition_lab_browser.NutritionLabBrowserTests
```

The offline browser test uploads the real repository image while stubbing cloud
services and the OpenAI response. It proves browser integration, not model
accuracy. The explicit live target resets the synthetic account and makes three
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
layout, and logged-out denial. It produces `nutrition-lab-estimate-mobile.png`,
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
