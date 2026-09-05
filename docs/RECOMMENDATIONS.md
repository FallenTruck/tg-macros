# Nutrition Phase 2.3 recommendations

## Production sequence

The Telegram callback transaction confirms the action and persists final meal
macros in DynamoDB. `confirmed_nutrition_payload` rereads the confirmed meal and
rebuilds that local day's totals from strongly consistent confirmed-meal queries.
It never uses the pending estimate for consumption. Historical days use their
target revision where available; legacy profiles fall back to the saved target.

The worker sends **Message 1**, containing the meal, daily consumption, targets
and remaining macros. Its message ID is recorded on the confirmed action. It then
acknowledges/removes the old controls and prepares a separate recommendation.
Candidate preparation, model ranking, formatting and **Message 2** delivery are
isolated: none can roll back or make SQS retry confirmation. Cleanup/ack failures
also cannot block recommendations. A failed Message 1 can be retried through the
existing idempotent confirmation and receipt check. Message 2 is best effort;
a process interruption after the receipt may omit it. Telegram send and DynamoDB
receipt writes cannot be atomic, so an ambiguous send/receipt failure can still
duplicate Message 1. This is not an exactly-once delivery guarantee.

The expiry sweep uses the same persisted-state message, then fast deterministic
recommendations, avoiding a model timeout per expired action. It never sends to
the synthetic Telegram sentinel. As before, sweep notification failures do not
retry the durable confirmation; they are logged. Callback confirmation uses the
hybrid model path. No SQS queue, lease, DLQ or estimator policy changes are needed.

Telemetry events are `nutrition_message1_sent`, `nutrition_recommendation_started`,
`nutrition_recommendation_model_ranked`, `nutrition_recommendation_fallback`,
`nutrition_recommendation_skipped`, and `nutrition_message2_sent/failed`.
Failure records include only stage, error class and user fingerprint, not meal
contents or SDK exception text. Receipt/old-message failures have separate events.

## Local timing and occasion

Version: `nutrition-recommendation-v3`. Bedtime is **23:30 in the saved profile
timezone**. A clock can be injected in tests; production uses the service clock.
Minutes are floored, and exactly 180/90/45/15 minutes enter the stricter band.
At/after bedtime, minutes remain nonpositive; 00:00–04:59 uses the preceding
night's bedtime rather than treating midnight as a fresh full-meal window.
Daily macro accounting still uses the existing local calendar-day boundary.

| Minutes until bedtime | Band | Default approach |
| --- | --- | --- |
| >180 | full_meal | Normal macro fit; proper meal allowed |
| >90 to 180 | moderate | Moderate meal; large/fatty options penalized |
| >45 to 90 | light | Light meals and protein options preferred |
| >15 to 45 | top_up | Small protein top-up preferred |
| <=15, including after bedtime | bedtime | No default full meal; small top-up only for a meaningful gap |

Occasion is morning meal before 11:00, lunch until 14:00, afternoon snack until
17:00, then dinner. Within 90 minutes of bedtime it is a late-evening snack/top-up.
A meal within the previous two hours shifts lunch toward afternoon snack, or
dinner toward a later top-up. Unknown/ambiguous timestamps do not imply a specific
meal. Future meal timestamps do not become the most recent meal. Legacy naive
meal times are interpreted in the profile timezone; stored UTC times are converted.

Plausible remaining occasions are one within 180 minutes, otherwise
`min(4, max(1, minutes // 240 + 1))`. This is a rough planning signal, not a schedule.
The request includes current local datetime, dated bedtime, minutes, band,
occasion/count, last meal time, today's meal count, up to six recent confirmed
meal summaries with local timestamps/macros, totals, remaining macros and preferences.

## Catalogue and restrictions

Each candidate has an explicit `meal_type`: `full_meal`, `light_meal`, `snack`, or
`protein_top_up`. `tags` describe heavy/light, high fat/protein, low fat, cuisine
and late-evening suitability. `contains` records declared ingredient categories;
`available` can disable an entry. Legacy entries default conservatively to
`full_meal`; model-generated names never classify meal types.

The 16 generic dishes no longer carry personal Telegram allowlists. Six additions
cover nonfat Greek yogurt, whey/water, soy protein/water, tofu soup, lentil/tofu
and lean chicken with a smaller rice portion. Vegetarian and vegan options span
Asian, Indian and Western choices. Egg/toast has an explicit `ovo_vegetarian` tag: it is eligible for vegetarian
profiles only when `eggs_allowed=true`. Existing vegetarian profiles without an
egg allowance keep their previous exclusion. The generic catalogue is therefore usable by
`javaan-e2e` and real profiles with matching restrictions, without an ID bypass.
Personal catalogue-growth approvals still retain their explicit per-user scope;
nonempty `eligible_telegram_user_ids` remains enforced for those entries.

Hard filtering occurs before scoring/model ranking. The saved profile owns two
separate sets of rules:

| Hard constraints | Soft ranking preferences |
| --- | --- |
| `diet_type`: `vegetarian`, `non_vegetarian`, `vegan` | `preferred_cuisines` |
| `eggs_allowed`, `dairy_allowed`: boolean or null | `preferred_staples` |
| `allergens`, existing `restrictions` | `preferred_meal_styles`, existing `preferred_tags` |
| `forbidden_ingredients` | `commonly_eaten_foods` |
| `forbidden_foods`: catalogue IDs or exact names | `avoided_foods` |
| Legacy explicit free-from/allergy flags | `variety_preference`: `balanced`, `high`, `low` |

The authenticated profile API reads/saves these fields and preserves omitted
fields during ordinary questionnaire/target updates. Arrays accept up to 32
nonempty strings of at most 100 characters; allowances require actual JSON
booleans/null, never truthy strings. No new frontend form controls are part of
this backend change. File-backed profiles and identity migration retain the same
fields. No real-user diet values are changed by deployment.

An empty `diet_type` uses the legacy saved vegetarian/vegan flags. An explicit
diet type takes precedence over legacy preference labels; restriction flags still
apply. Vegetarian profiles default to no eggs unless explicitly allowed;
non-vegetarian status permits, but never requires, meat. Vegan rules still forbid
egg/dairy even when allowance fields conflict. Explicit bans/allergens always
win over allowances. Free-from/allergy flags previously saved in
`dietary_preferences` retain their hard meaning. Macro fit, time and preferences
never participate in eligibility and cannot reinstate a forbidden candidate.

Known ingredient evidence (`contains` plus `ingredients`, normalized aliases and
category relationships such as whey→dairy) defeats contradictory free-from tags.
Allergens and restriction flags require affirmative `X_free` tags; unknown flags
fail closed without a matching affirmative tag. For egg/dairy allowance bans,
legacy vegetarian/vegan tags supply their established absence guarantees unless
contradicted by known ingredients. Explicit ingredient bans otherwise require an
affirmative absence tag or a nonempty, explicitly complete reference ingredient
list. Most composed dishes have partial lists because sauces, seasonings and
packaged subingredients are unknown; they cannot prove an arbitrary ingredient
absent. Only the specified plain yogurt and pure soy/water reference servings
currently mark their ingredient lists complete. This does not certify product
allergen cross-contact. Forbidden-food IDs/names use normalized exact matching;
use `forbidden_ingredients` to exclude an ingredient across different dishes.

The model receives the already-enforced hard context and bounded soft
preferences, and only valid candidate IDs. Invalid IDs in a response trigger
fallback from the valid set.
All serving macros are fixed **planning approximations**, not measured intake,
verified restaurant/package labels, or nutrition ground truth. Existing macros
are unchanged. Each new entry names its intended serving and declares its source
status in `nutrition_source`. Powder, yogurt and tofu values vary by product;
use a verified product-specific serving before treating them as precise. The
catalogue is independent of the photo estimator and accuracy corpus.

## Deterministic scoring

For each macro, closeness is `max(0, 1 - abs(value - desired) / tolerance)`.
Weights/tolerances for kcal, protein, carbs, fat are respectively 28/220, 26/18,
20/28, 12/12. Desired amounts use remaining gaps: kcal `clamp(.55*r,280,700)`,
protein `clamp(.6*r,18,50)`, carbs `clamp(.5*r,20,80)`, fat `clamp(.45*r,6,24)`.

Additional signals:

- +12 for >=25g candidate protein when >=30g remains; -18 for <20g protein in that case.
- +10 for protein density >=0.09g/kcal when >=30g protein remains.
- +10 when calories are within the remaining amount +120 kcal.
- Subtract `min(50, (candidate_kcal - remaining_kcal - 120)/10)` for calorie overshoot above that tolerance.
- -24 when <15g fat remains and candidate fat exceeds the remainder by >8g.
- -14 for >55g candidate carbs after confirmed daily carbs exceed 65% of target.
- +8 matching cuisine, +6 preferred style/tag, +6 preferred staple matched in the food ID, name or declared ingredients.
- +4 for a commonly eaten food; -12 for a softly avoided food (neither changes dietary eligibility).
- -18 per name token longer than three characters repeated in the last six meal captions, multiplied by 1.5 for high variety preference or 0.5 for low variety preference; balanced preserves 1.0.
- Style matches combine `preferred_tags` and `preferred_meal_styles` for the single +6 bonus. Equally fitting cuisines retain the +8 preferred-cuisine advantage.

Timing adjustments are additive. A candidate may incur several penalties:

| Band | Full meal | Heavy tag or >=600 kcal | High-fat tag or >=20g fat | >55g carbs | Light protein bonus |
| --- | ---: | ---: | ---: | ---: | ---: |
| full_meal | 0 | 0 | 0 | 0 | 0 |
| moderate | -10 | -15 | -12 | 0 | +8 |
| light | -40 | -35 | -30 | -25 | +30 |
| top_up | -65 | -50 | -40 | -35 | +40 |
| bedtime | -90 | -60 | -50 | -45 | +50 |

The bonus requires a light meal/snack/top-up with >=20g protein, <=12g fat and
<=450 kcal, reduced to <=250 kcal within 45 minutes. Within 45 minutes, an option
above 250 kcal also incurs -30. Full meals are scored rather than prohibited.
The existing usefulness cutoff removes scores below -25; the best five remaining
candidates go to ranking. Within 90 minutes, the highest deterministic candidate
is retained and ranked first even if the model omits it. The other model choices
remain validated alternatives. Output lists at most three alternatives, not a
plan to eat all three.

## Bounded model and suppression

`OPENAI_RECOMMEND_MODEL` still defaults to `gpt-4.1-mini`; no estimator model or
prompt changed. The recommendation client uses a strict candidate-ID schema,
20-second SDK timeout, no SDK retries and a 22-second coroutine bound. Returned
IDs must be valid, unique and supplied. Names/macros are rebound from the catalogue,
and daily totals and remaining gaps from application state. Invalid output or
model failure falls back deterministically. The summary is deterministic;
the model supplies short per-option reasons/tradeoffs; references to internal scores/models are replaced with deterministic wording. Meal facts/preferences
are bounded; raw conversation history and user identifiers are not sent.

Message 2 has no duplicate daily macro table. Historical dates, no valid/useful
candidates, all major macro targets exceeded, or <200 kcal and <20g protein
remaining suppress it. Within 45 minutes, <300 kcal and <20g protein also suppress
it. A >=20g protein gap can still suggest <=250 kcal protein options despite
very low remaining calories, explicitly noting calorie overshoot. A substantial
gap (>=500 kcal or >=30g protein) near bedtime gets an explicit tradeoff sentence.
Skipped results format as empty text and are never sent. The Lab retains its
current-day dashboard and adds a separate persisted-state Telegram preview;
queued historical recommendations are rechecked after midnight.

## Verification and limits

`make recommendation-benchmark` runs deterministic scenario properties and
production callback failure/order tests without OpenAI. The dietary scenarios include vegetarian with/without eggs, dairy restriction, prohibited ingredients, equal-fit Indian/Western meals, conflict precedence, model input safety and profile roundtrips. `make e2e-recommendations`
invokes only the gated dev Lab Lambda, resets/verifies only the marked synthetic
baseline, calls the deployed planner at 18:30 and 22:30, verifies actual suggestion
IDs/types and unchanged domain state, then restores the baseline. See
[E2E_TESTING.md](E2E_TESTING.md). The existing browser Nutrition Lab smoke exercises
full logging, correction, confirmation/recommendation and cancellation.

These are heuristic rankings, not meal scheduling, digestion predictions or
clinical recommendations. Timing is a fixed bedtime policy, hunger/availability
is not observed live, repeat matching uses caption tokens, and catalogue macros
are approximate. Recommendations are best effort; the persisted meal remains
authoritative regardless of delivery or model availability.
