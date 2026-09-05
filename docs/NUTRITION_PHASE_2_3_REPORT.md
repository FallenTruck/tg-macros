# Nutrition Phase 2.3 delivery report

## Post-log behavior

1. Confirm the action and persist final meal macros transactionally.
2. Reread the confirmed meal and rebuild its local-day totals from strongly consistent confirmed records.
3. Send the deterministic nutrition-state message and record its delivery receipt.
4. Acknowledge/remove the old meal controls.
5. Prepare the profile-local timing context and valid catalogue candidates; rank with the bounded model or deterministic fallback.
6. Send a separate recommendation only when useful. Historical/skipped results send nothing. Recommendation preparation, formatting or delivery failures cannot retry or undo confirmation.

Message 1 example from the deterministic callback test:

```text
✅ Meal logged

This meal
640 kcal
P 43g · C 70g · F 20g

Today
1,420 / 2,100 kcal

Protein
86 / 150g

Carbs
145 / 220g

Fat
48 / 70g

Remaining
680 kcal
64g protein · 75g carbs · 22g fat
```

Message 2 example (abridged to one option; production supports three):

```text
🥗 What to eat next

About 60 minutes until your 23:30 bedtime; keep the next intake lighter.
A substantial gap remains; a small top-up helps, but may not cover it all tonight.
Protein still needs attention.

1. Double Protein Shake
2 scoops whey with milk
~380 kcal · 48g protein
Protein-efficient option.
```

## Recommendation policy

Bedtime is 23:30 in the saved timezone. The five deterministic bands are >180,
90–180, 45–90, 15–45 and <=15 minutes, with exact boundaries entering the stricter
band. They progress from normal meals through moderate meals, light intake and
small protein top-ups. After-midnight times before 05:00 remain near the previous
night's bedtime. Significant late protein/calorie gaps are explained, not ignored.

Likely occasion uses morning/lunch/afternoon/dinner clock bands, the most recent
confirmed local meal time, and bedtime proximity. A meal in the last two hours
shifts the next occasion toward a snack/top-up. The request also includes a rough
count of remaining eating occasions and bounded confirmed meal/time/macro facts.

Catalogue meal types are explicit: `full_meal`, `light_meal`, `snack`,
`protein_top_up`. Tags, declared ingredient categories and availability support
hard filtering and timing scores. Heavy/large, high-fat and large-carb meals get
increasing late penalties; small protein-efficient choices receive bonuses.
Protein gaps, low fat remaining, carb-heavy days, preferences and repetition
remain scoring inputs. Exact weights and boundaries are documented and tested in
[RECOMMENDATIONS.md](RECOMMENDATIONS.md).

The generic catalogue grows from 16 to 22 entries, removing all generic-food
Telegram ID allowlists. Personal approved catalogue additions retain explicit
scope support. The synthetic account receives suggestions through the same
restriction/availability path, without a real-user eligibility bypass.
Vegetarian/vegan restrictions are enforced before ranking; unknown hard
restrictions fail closed. Egg/toast retains its previous vegetarian exclusion.

The model remains `gpt-4.1-mini`. It chooses only supplied candidate IDs and short
reasons/tradeoffs. Application state owns all returned totals and food macros.
Within 90 minutes of bedtime the strongest deterministic candidate stays first.
Internal scoring terminology is replaced with deterministic explanations.

## Validation evidence

- Full offline suite: 274 tests passed.
- Focused deterministic benchmark and production message tests: 25 tests passed.
- Offline Nutrition Lab browser integration: one test passed.
- `git diff --check`, Python compile checks, `make sync-runtime`, runtime mirror checks and `sam validate --lint`: passed.
- Colima Linux/arm64 `sam build --use-container`: succeeded, including the final wording/Unicode changes.
- Final backend deployment: `sam deploy` succeeded; `tg-macros-dev` independently verified `UPDATE_COMPLETE` in `ap-southeast-1`.
- Deployed recommendation scenarios: both `model_ranked`; 18:30 selected Salmon Rice Plate first, 22:30 selected Double Protein Shake, Tofu Clear Soup and Plain Nonfat Greek Yogurt. The entire synthetic domain partition was unchanged during the calls.
- Live Nutrition Lab: one test passed (101.5 seconds), including real estimate-only, corrected confirmation/recommendation, cancellation and logout. The estimator stayed `nutrition-estimator-v2 / gpt-5.4`; confirmation returned actual model-ranked suggestions and a separate state preview.
- Final deployed app smoke: one test passed (62.4 seconds), covering browser auth/session/logout, nutrition history, responsive navigation and the full synthetic workout flow.
- All live activity used only the marked synthetic account; no real-user automated messages were sent. Synthetic baseline cleanup followed testing.
- Frontend files were unchanged; no Mini App deployment was needed.

The scenario benchmark covers all ten requested cases: dinner protein deficit,
60-minute bedtime protein deficit, low fat remaining, carb-heavy day, very low
calories remaining, vegetarian restriction, repeated recent foods, historical
log, nearly met targets and no valid candidates. It also tests timezones,
thresholds, after-midnight handling, explicit eligibility/availability, model-ID
validation, exact timing scores, metadata roundtrips and bounded prompts.

Message tests cover persisted final macros, exclusion of pending food, split
formatting, UTF-16 Telegram length limits, first-message retry, duplicate receipt,
automatic confirmation, historical suppression and failures in recommendation
preparation, ranking, formatting, send, callback acknowledgement and old-message
cleanup. Existing SQS/idempotency, photo/correction, authentication, dashboard,
workout, Lab and accuracy-tool tests remain in the full suite.

## Changed files

- `lambda_handlers/worker.py`: ordered messages, receipt handling, failure isolation, telemetry and expiry fallback.
- `macro_bot/serverless_service.py`, `macro_bot/serverless_data.py`: persisted confirmation payload, receipt methods, consistent meal reads and clock propagation.
- `macro_bot/recommendations.py`, `macro_bot/models.py`, `food_catalog.json`: timing, restrictions, scoring, bounded ranking, metadata and six reference servings.
- `macro_bot/formatting.py`, `macro_bot/handlers.py`: split formatters, Unicode length limit and skipped-message handling for the legacy adapter.
- `macro_bot/nutrition_lab.py`, `lambda_handlers/lab_worker.py`: separate state preview, queued historical recheck and gated read-only scenario operation.
- `scripts/recommendation_smoke.py`, `Makefile`, `e2e/test_live_app.py`: synthetic deployed checks and stronger live confirmation assertions.
- `tests/test_recommendation_scenarios.py`, `tests/test_post_log_messages.py`, plus existing recommendation, formatting, Lab and adapter tests.
- `docs/ARCHITECTURE.md`, `docs/E2E_TESTING.md`, `docs/RECOMMENDATIONS.md`, and this report.
- Corresponding checked-in mirrors under `lambda_handlers/runtime/`.

Photo estimator, estimator prompt/model/preprocessing, correction math, ground
truth tooling, frozen accuracy benchmark, Mini App assets, browser auth, workout
implementation and infrastructure template were not changed.

## Known limits

Timing/occasion and repetition rules are heuristics; catalogue serving macros
are planning approximations, and availability is curated rather than live.
23:30 is currently fixed, and daily totals still reset at local midnight.
The catalogue does not certify allergen cross-contact or predict digestion.
Model prose can vary, and only five scored candidates reach the model.

Message 2 is best effort and may be omitted after an interruption. Telegram send
and its DynamoDB receipt are not atomic; an ambiguous first-message send or
receipt failure can duplicate Message 1. Scheduled expiry uses deterministic
suggestions to bound latency, and notification failures do not replay expiry.
The durable confirmed meal remains authoritative in each case.

The delivery message records the final commit hash and push result.
