# E2E Nutrition Lab checklist completion — 2026-09-06

The deployed Lab is an evaluation harness, available at the Mini App's hidden
`#nutrition-lab` route only to the marked `javaan-e2e` browser session. It is not
an end-user meal-entry feature. The production estimator, preprocessing,
reconciliation, correction math and recommendation planner remain shared.

| # | Requested report item | Implementation and evidence |
| --- | --- | --- |
| 1 | Shared analysis | Both Telegram and Lab call `NutritionService.analyze_meal_image`; adapter parity test compares actual estimator requests/results with only OpenAI stubbed. |
| 2 | API routes | Gated asynchronous `GET/PUT /api/e2e/nutrition-lab/jobs[/id]` and `POST .../id/{correct,confirm,cancel}`. Async jobs avoid the HTTP API timeout; route details are in E2E_TESTING.md. |
| 3 | Authorization | Every endpoint requires browser authentication plus canonical marker, identity, reserved ID and credential mapping. Worker revalidates the same boundary. Normal/Telegram users are denied. Body identity overrides and all query parameters are rejected. |
| 4 | Dev enablement | Explicit flag plus exact dev environment, stack `tg-macros-dev` and region `ap-southeast-1`. SAM parameter defaults off. Dev-only resources/permissions are conditional. |
| 5 | Estimate-only | Complete structured estimate, model/usage, application version, production preview and measured latency. All temporary job writes are in separate `NutritionLabJobs`; no nutrition-table/user-partition writes. |
| 6 | Full log | Existing durable pending meal/action, original estimate and model metadata; correction, confirm/cancel and existing deadline/idempotency semantics. No Telegram messages. |
| 7 | Correction parity | Tests compare Lab and shared service results for base, skin, light/heavy sauce and smaller/larger whole portion. Preview refreshes with the corrected estimate. |
| 8 | Recommendation parity | Same planner reads confirmed daily state. Current daily totals, payload and production preview are returned. Generation/dispatch failure preserves confirmation. Offline past-date test verifies saved-user timezone, correct local date and current-day recommendation suppression. |
| 9 | Version ownership | `validate_result` unconditionally assigns `ESTIMATOR_APPLICATION_VERSION`; regression tests reject reliance on arbitrary model version text. Live results report `nutrition-estimator-v2` in both estimate and usage. |
| 10 | Browser route/UI | Hidden `#nutrition-lab`, separate from normal Nutrition and four-item navigation. Backend capability controls rendering. Readable totals/ranges, items, assumptions/confidence, previews, Adjust controls, confirmed state, recommendation and downloadable JSON. Required test selectors are present. |
| 11 | Playwright | Dedicated real-image test navigates to the hidden route, verifies version/model/items/reconciliation/preview/timing, corrects and confirms, checks today's meal macros, cancels another run, checks responsive overflow and logout denial. |
| 12 | Fixtures | Existing test-safe breakfast image `images/6143401176322477320.jpg`, with the user-confirmed mac-and-cheese caption. Additional explicitly authorized submissions are kept only in the ignored private corpus. |
| 13 | No estimate persistence | Offline test compares the entire nutrition fake table unchanged. Live test compares the entire E2E user partition, without excluding job records. The private variance runner repeats this before/after check. |
| 14 | Full-log isolation | Offline table diff contains only the E2E user partition; dedicated worker IAM restricts nutrition writes to `USER#e2e-javaan-e2e`. Live calls authenticate only as that account. Importing authorized reference photos performs read-only operations against source accounts. |
| 15 | Telegram regression | Full offline suite includes adapter/photo download, shared estimation, pending action, correction, confirmation, recommendation, duplicate-delivery and timeout coverage. |
| 16 | Offline results | 214 unit tests passed; separate Chromium integration test passed. |
| 17 | SAM validation/build | `git diff --check`, `node --check`, runtime sync, `sam validate --lint`, and Colima-backed `sam build --use-container` passed. |
| 18 | Backend deployment | SAM deployment succeeded; dev stack verified `UPDATE_COMPLETE`. Retained nutrition table was preserved. |
| 19 | Mini App deployment | `make deploy-miniapp` succeeded; CloudFront invalidation `IEJNFH8JWXYX7MTX68T7KR0FN7` completed. |
| 20 | Live results | Standard app smoke passed (53.793 s); dedicated Lab test passed (102.086 s, three actual image/estimator calls). Private variance batch: 16 successful calls, two per eight submitted images, unchanged user partition. |
| 21 | Screenshots | Post-authentication `artifacts/e2e/nutrition-lab-estimate-mobile.png`, `nutrition-lab-confirmed-mobile.png`, and `nutrition-lab-desktop.png`. Mobile estimate/confirmation captures and offline desktop capture were visually inspected. |
| 22 | Commit | Exact release commit is reported in the task completion message; this report is included in that commit. |
| 23 | Push | Push to `origin/main` and remote-head/clean-worktree verification are reported in the completion message. No force push. |
| 24 | Limits | Harness correctness and repeatability are measured; nutritional accuracy remains unknown without defensible weighed/recipe labels. Two repeats are an initial sample. This live confirmation returned the planner's deterministic no-suggestions result (`source=skipped`), not a generated recommendation list. |

The live full-log test resets and verifies the deterministic baseline before the
scenario and in cleanup after it, including on assertion failure. A final read
verified one baseline meal (600 kcal, P 40 g, C 60 g, F 15 g) with no correction
records after the private variance batch. Reset and full-log scenarios must run
sequentially. Reset rotates a profile revision checked by queued/in-flight jobs;
temporary jobs remain outside nutrition state and expire after 24 hours.

Local evaluation data is intentionally excluded from Git:

- `artifacts/e2e/nutrition-lab-live-results.json`: the three structured live results.
- `artifacts/nutrition/corpus.sqlite3`: persistent registry and repeat history.
- `artifacts/nutrition/private/manifest.json`: combined public/private registry.
- `artifacts/nutrition/private/variance-report.json`: the private repeat report.

Eight additional submissions were imported with explicit account authorization,
visually inspected and tagged for relevant differences (including screenshots,
product imagery, shared plates, packaging and alternate framing). Source captions
were preserved. Private images, captions and reference metadata were not added
to Git, and existing model estimates were not promoted to nutrition ground truth.
