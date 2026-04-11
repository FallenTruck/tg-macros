# Suggested Improvement: Preference Inference From Logged Meals

## Summary

This document parks a future improvement for automatically inferring user food preferences from confirmed meal logs.

The current system stores:

- questionnaire-derived macro targets
- manually migrated profile preferences for the two known users
- confirmed meal history in `meals_v2.csv`
- recurring-meal suggestions in `catalog_suggestions.json`
- approved food catalog entries in `food_catalog.json`

For new users, the Mini App currently saves only macro-target data. Preference arrays start empty:

- `dietary_preferences`
- `restrictions`
- `preferred_cuisines`
- `preferred_staples`
- `preferred_tags`

The proposed improvement is to infer low-risk preferences after a user has enough meal history, then write those suggestions back to the user profile or surface them for confirmation.


## Why This Matters

Recommendation quality improves significantly if the system knows what a user tends to eat.

Today, the recommendation layer already uses profile preferences in scoring:

- cuisines
- tags
- staples
- restrictions

Relevant code paths:

- `macro_bot/recommendations.py`
- `macro_bot/catalog_growth.py`
- `macro_bot/storage.py`
- `macro_bot/models.py`

Without inferred preferences, new users have weak personalization until they manually edit profile data.


## Current State

### Data already available

1. Confirmed meal logs
   - Stored in `meals_v2.csv`
   - Include `telegram_user_id`, `caption`, macros, timestamp

2. User profiles
   - Stored in `user_profiles.json`
   - Include preference arrays and targets

3. Food catalog
   - Stored in `food_catalog.json`
   - Entries may contain:
     - `tags`
     - `cuisines`
     - `eligible_telegram_user_ids`

4. Recurring cluster suggestions
   - Built in `macro_bot/catalog_growth.py`
   - Can identify meals a user repeatedly logs

### Limitation

New users who complete the Mini App questionnaire still have empty preference arrays unless someone updates them later.


## Recommendation

Use a deterministic inference layer first. Do not use an AI model as the primary inference engine.

Reason:

- the repo already has structured signals
- deterministic inference is testable and debuggable
- meal histories are small and noisy
- restrictions are high-risk to infer incorrectly

Optional later enhancement:

- use an AI model only to summarize evidence or produce user-facing suggestions
- do not let a model silently write restrictions


## Desired Outcome

After a user logs enough confirmed meals, the system should infer:

- `preferred_cuisines`
- `preferred_staples`
- `preferred_tags`
- possibly `dietary_preferences`

It should not silently infer hard restrictions such as:

- `vegetarian`
- `vegan`
- allergies
- religious restrictions

At most, those should become suggestions for explicit user confirmation.


## Proposed Inference Flow

### Step 1: Wait for enough data

Minimum threshold before inference:

- at least `10` confirmed meals
- spanning at least `7` distinct days

If a user does not meet the threshold, do nothing.

### Step 2: Build evidence set

For the target `telegram_user_id`, gather:

1. Recent confirmed meals from `MealLogRepository`
2. Approved catalog entries that match recurring meals
3. Existing pending or applied catalog suggestions if needed as a fallback signal

Priority of evidence:

1. Approved food catalog matches
2. Repeated recurring-meal clusters
3. Raw caption token heuristics

### Step 3: Infer cuisines

If a logged meal maps to a catalog entry with cuisines, count those cuisine labels.

Suggested rule:

- count cuisine occurrences across the last `20` meals
- keep cuisines seen in at least `3` meals
- require cuisine share >= `25%` of matched meals

Example:

- `indian` appears in `7` of `18` meals -> include
- `western` appears in `1` of `18` meals -> exclude

### Step 4: Infer tags

Use catalog tags on matched meals and recurring catalog suggestions.

Suggested safe tags for auto-inference:

- `meal`
- `snack`
- `high_protein`
- `indian`
- `asian`
- `vegetarian` only as a soft preference tag, not a hard restriction

Suggested rule:

- require tag in at least `3` meals
- require tag share >= `20%`

### Step 5: Infer staples

Staples should come from repeated foods or ingredients visible in:

- approved catalog entry names
- approved catalog suggestion names
- normalized caption tokens

Examples:

- `rice`
- `chicken`
- `eggs`
- `paneer`
- `dosa`
- `chapati`
- `curd`

Suggested rule:

- tokenize normalized meal names
- remove stopwords, quantities, units, and generic words
- keep tokens appearing in at least `3` distinct meals
- keep at most top `5-7` staples

Possible reusable logic:

- adapt token normalization already present in `macro_bot/catalog_growth.py`

### Step 6: Infer soft dietary preferences

This field is less structured today, so it should be conservative.

Possible values:

- `high_protein`
- `mixed_diet`
- `vegetarian`
- `indian`

Suggested rule:

- only write values that are supported by repeated cuisine/tag evidence
- treat these as soft preferences, not restrictions

### Step 7: Restrictions remain manual

Do not auto-write `restrictions`.

Alternative:

- if evidence is strong, create a suggestion like:
  - "This user appears vegetarian based on 15 recent meals. Ask for confirmation."


## Suggested Implementation Shape

### New service

Add a dedicated module, for example:

- `macro_bot/preference_inference.py`

Suggested responsibilities:

- load recent meals for a user
- map meals to catalog/caption evidence
- compute inferred preferences
- update or suggest profile changes

### Suggested dataclasses

Possible new dataclasses:

```python
@dataclass(frozen=True)
class PreferenceInferenceResult:
    telegram_user_id: int
    inferred_cuisines: list[str]
    inferred_staples: list[str]
    inferred_tags: list[str]
    inferred_dietary_preferences: list[str]
    restriction_suggestions: list[str]
    evidence_summary: dict[str, object]
```

```python
@dataclass(frozen=True)
class PreferenceInferenceStats:
    meal_count: int
    distinct_days: int
    matched_catalog_meals: int
```

### Suggested public entry points

```python
class PreferenceInferenceService:
    def infer_for_user(self, telegram_user_id: int) -> PreferenceInferenceResult | None:
        ...

    def apply_for_user(self, telegram_user_id: int) -> PreferenceInferenceResult | None:
        ...
```

Behavior:

- `infer_for_user`: return inferred result without mutating profile
- `apply_for_user`: write safe inferred fields to profile


## Profile Update Policy

Safe auto-updates:

- `preferred_cuisines`
- `preferred_staples`
- `preferred_tags`
- optionally `dietary_preferences`

Do not overwrite explicit manual data blindly.

Suggested merge behavior:

1. If the profile field is empty, fill it with inferred values
2. If the profile field already exists, either:
   - merge inferred values with existing values
   - or store inferred values separately for review

Best first version:

- only auto-fill empty fields
- do not mutate non-empty manual preferences


## Trigger Options

### Option A: Trigger after confirmed meal logging

In `macro_bot/handlers.py`, after a meal is confirmed:

- check whether user has enough history
- run inference
- update profile if appropriate

Pros:

- always fresh
- no manual admin action needed

Cons:

- extra work on every confirmation

### Option B: Periodic/manual refresh

Add a script or admin function that runs:

- per user
- or for all users

Pros:

- simpler rollout
- easier to inspect results

Cons:

- not real-time

Recommended first rollout:

- periodic/manual refresh first
- automatic trigger later after confidence is good


## Use of AI Models

Not recommended for first-pass inference.

Recommended limited AI usage later:

1. Summarize evidence for display
2. Generate a short user-facing explanation:
   - "You seem to eat Indian vegetarian meals often. Save these preferences?"
3. Produce review suggestions for possible restrictions, but never auto-apply them

If an AI layer is added, it should consume structured evidence from the deterministic layer, not raw meal history directly.


## Testing Plan

Add tests covering:

1. No inference when meal count is too low
2. Cuisine inference from repeated catalog-linked meals
3. Staple inference from repeated normalized tokens
4. Tag inference from repeated catalog tags
5. No restriction auto-write
6. Merge behavior when profile fields are already populated
7. Cross-user isolation by `telegram_user_id`

Likely test files:

- new `tests/test_preference_inference.py`
- possible integration coverage in `tests/test_handlers_integration.py`


## Rollout Plan

### Phase 1

- implement deterministic inference service
- support dry-run inference only
- inspect results manually for current users

### Phase 2

- apply safe inferred fields to empty profile arrays
- keep restrictions manual

### Phase 3

- add Mini App section or bot prompt to review inferred preferences
- optional AI-generated explanation layer


## Open Questions

1. Should inferred preferences overwrite existing values or only fill blanks?
2. Should `dietary_preferences` remain distinct from `preferred_tags`?
3. Should recurring raw captions be promoted into catalog entries first, then used for inference?
4. What minimum history threshold gives stable results for low-volume users?
5. Do we want a user-visible review flow before any inferred values are persisted?


## Strong Recommendation

Implement deterministic inference first and keep restrictions manual.

The repo already has enough structure to infer meaningful cuisines, staples, and tags without introducing a model-driven decision layer. AI can be layered later as an explanation tool, not the source of truth.
