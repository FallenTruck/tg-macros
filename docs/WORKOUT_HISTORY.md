# Workout history

## Inspection and decision

The existing architecture is Mini App → authenticated FastAPI Lambda →
NutritionService → WorkoutExecutionRepository → DynamoNutritionRepository's
DynamoDB primitives. Canonical modules are mirrored into `lambda_handlers/runtime`
by `make sync-runtime`.

Existing records all use `PK=USER#<internal-user-id>`:

| Record | SK |
| --- | --- |
| Active pointer | `WORKOUT#ACTIVE` |
| Session | `WORKOUT#<actual_local_date>#<session_id>` |
| Execution | `<session SK>#EXEC#<sequence:03d>` |
| Set | `<execution SK>#SET#<ordinal:03d>` |

The active pointer already provides a direct session key. Inactive lookup formerly
queried the entire user partition and searched for a matching session ID.
The table has no GSI. Querying `WORKOUT#` alone also reads executions and sets,
and date-first session keys do not order sessions by start time within a day.
Neither access path satisfies a bounded, summary-only history query.

Selected design: two small materialized records in the existing primary index.
This preserves all original keys, supports strongly consistent reads, and avoids
a GSI rollout and its eventual consistency. No infrastructure or IAM changes are
required. The tradeoff is one locator per session and one summary per closed
session, maintained by the same transactions as their source records.

| Added record | SK | Contents |
| --- | --- | --- |
| Direct locator | `WORKOUT_SESSION#<session_id>` | Original session SK |
| Closed summary | `WORKOUT_HISTORY#<UTC-start-with-fixed-microseconds>#<session_id>` | Session metadata only |

Both use the authenticated user's PK. Session IDs break equal-time ties in
reverse lexical order. Start timestamps are normalized to UTC for index ordering;
original displayed dates/timestamps remain unchanged. Completed and cancelled
sessions are included; in-progress sessions are excluded. Cancellation continues
to use the existing `completed_at` end timestamp convention.

## Runtime changes

`WorkoutExecutionRepository.list_workout_history` directly executes one DynamoDB
Query with `begins_with(SK, WORKOUT_HISTORY#)`, `ScanIndexForward=False`, a limit
of 1–50 (default 20), and `ConsistentRead=True`. It does not use the nutrition
query helper, which consumes continuation keys internally. No executions, sets,
FilterExpression, scan, or unrelated user records enter this list operation.

The API cursor is canonical, versioned URL-safe base64 JSON around the last
key. It is bounded, syntax/date validated, restricted to the history prefix and
bound to the authenticated PK. It is opaque API state, not encrypted or signed;
tampering can only reposition a query within the caller's own history. No client
PK is trusted for a DynamoDB read. Duration is derived from nonnegative timestamp
differences and floored to whole minutes.

Session starts atomically write a locator. Completion/cancellation atomically
write their final summary alongside the existing lifecycle updates and pointer
removal. Historical detail uses a locator GetItem and original session GetItem,
then the existing session-scoped execution/set queries. These detail queries
still have the existing per-exercise set reads; they never search other user
records. Names come from the saved programme version, with stored IDs as fallback.
Historical reads do not change data or pointers; existing closed-session mutation
guards remain in effect. Existing logging race/conflict issues are outside scope.

API:

- `GET /api/workout/history?limit=20&cursor=...`: `{sessions, next_cursor}`.
- Existing `GET /api/workout/sessions/{session_id}`: session, executions, sets.
- Invalid limit/cursor: 400; absent or another user's session: identical 404.
- Both use existing authentication and no-store responses. No status filter is
  offered because it would discard query results under this key design.

The Mini App adds a History button, paginated list, loading/empty/error states,
retry and back navigation, and a separate read-only historical renderer. The
active session remains separate in state. The completion dock and editable
session view are hidden in history mode. Details include saved load/reps/time,
side reps, RIR, warm-up/working set type and skipped exercises/sets.

```text
Mini App
  ├─ GET /api/workout/history
  │    → authenticated API → service → workout repository
  │    → USER#id + WORKOUT_HISTORY# query (one descending page)
  └─ GET /api/workout/sessions/{id}
       → authenticated API → service → workout repository
       → USER#id + WORKOUT_SESSION#id GetItem
       → original session + scoped executions + saved sets
```

## Retained data and rollout

The repository documents an existing deployed development stack. A read-only
CloudFormation inventory attempt on 2026-09-13 failed because the `fitness-dev`
SSO token had expired. Live historical counts are therefore unknown. Treat
retained history as present. No AWS writes or deployment were performed for this
implementation.

The new API deliberately has no broad-query compatibility fallback. Existing
inactive records require the additive backfill before the history UI release.
The active-pointer path keeps old unfinished workouts usable during rollout.

1. Renew AWS SSO and discover `FitnessDataTableName` from CloudFormation outputs.
2. Run `scripts/backfill_workout_history.py --table-name <discovered-table>
   --profile fitness-dev --dry-run` to inventory missing derived records.
3. Deploy the backend through the normal SAM workflow; wait for older Lambda
   invocations to finish before backfilling so all new writes maintain summaries.
4. Run the same command with `--apply`. Repeat dry-run until `missing` is zero.
5. Deploy the Mini App using the normal versioned asset deployment, then verify
   history with the synthetic-account workflow in E2E_TESTING.md.

During the backend/backfill interval, legacy inactive detail returns 404 until
its locator is populated; coordinate this interval as a release maintenance step.
Do not announce the new history feature until backfill verification completes.

The migration is dry-run by default. `--user-id <internal-id>` limits discovery
to that user's original workout prefix; otherwise the offline administrative
job scans for session entities, following all DynamoDB continuation keys. This
scan exists only in the migration, never in the application request path. It
prints aggregate counts without identities or workout contents. Each session is
re-read consistently, and missing derived items use conditional create-only
writes. Existing matching items are left alone; conflicting ones fail without
overwrite. Repeated/interrupted runs are safe. Original sessions, lifecycle,
executions, sets, and active pointers are never changed by the migration.

The original execution and set keys/IDs remain intact for future exercise
history or analytics access patterns; those features are not implemented here.

## Validation

`tests/test_workout_history.py` covers closed statuses, active exclusion,
start-time and tie ordering, limits/cursors, page continuation, isolation,
scoped reads, reconstructed read-only detail, API errors, and additive backfill.
`e2e/test_workout_history_browser.py` exercises the real API with isolated fake
storage in offline Chromium, including 21-session pagination, empty/error/retry,
saved sets and hidden live-workout controls. Existing workout tests are retained.

Run:

```sh
make sync-runtime
node --check miniapp/app.js
.venv/bin/python -m compileall -q macro_bot lambda_handlers
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m unittest e2e.test_workout_history_browser e2e.test_core_workout_browser
sam validate --lint
git diff --check
```

Final local results (2026-09-13):

- Full repository suite: **314 tests passed**, 21.131 seconds.
- Offline history and existing core-workout browser regression: **2 passed**.
  History was rerun after the final display changes: **1 passed**, 5.642 seconds.
- SAM lint validation, JS syntax, Python compilation, runtime mirror checks and
  `git diff --check`: passed.
- Mobile history-detail screenshot inspected locally at
  `artifacts/e2e/workout-history/detail.png` (ignored test artifact).
- No deployed browser smoke test, live backfill or DynamoDB integration run:
  live inventory was blocked by expired SSO. Storage unit tests use the existing
  repository fakes, extended with continuation-key and query-scope assertions.

Files changed:

| File | Change |
| --- | --- |
| `macro_bot/workout_execution.py` | Summary/locator writes, paginated list, direct historical detail |
| `macro_bot/serverless_service.py` | Explicit history service operation |
| `lambda_handlers/api.py` | Authenticated history route |
| `lambda_handlers/runtime/macro_bot/workout_execution.py` | Synchronized runtime mirror |
| `lambda_handlers/runtime/macro_bot/serverless_service.py` | Synchronized runtime mirror |
| `lambda_handlers/runtime/api.py` | Synchronized runtime mirror |
| `miniapp/app.js` | History navigation, list/detail renderer, pagination, error recovery |
| `miniapp/index.html` | History entry point and container |
| `miniapp/styles.css` | History rows and detail spacing |
| `scripts/backfill_workout_history.py` | Dry-run/create-only migration |
| `tests/test_workout_history.py` | Seven repository/service/API/migration test scenarios |
| `e2e/test_workout_history_browser.py` | Real browser/API history flow |
| `docs/WORKOUT_HISTORY.md` | Design decision, release procedure, validation and limitations |

## Backfill execution — 2026-09-13

Following the user's explicit backfill request, AWS SSO was renewed and the
retained table was discovered through the `tg-macros-dev` CloudFormation stack:
`tg-macros-dev-fitness-data` in `ap-southeast-1`.

The initial live dry-run identified one retrospective completed session whose
`started_at` and `completed_at` are intentionally null. History indexing now
uses the saved `actual_local_date` at midnight UTC solely as a deterministic
ordering anchor for such entries. It preserves null actual timestamps and does
not derive duration. Exact ordering within that date is unknown. A regression
test covers migration, detail retrieval and preservation of this record shape.
The workout/history/runtime test suite passed: **37 tests**.

The subsequent dry-run found **13 sessions** (9 completed, 4 cancelled), requiring
**26 derived records**. The applied migration created all 26 (13 locators and
13 summaries). Verification found **zero missing records**. All **183 original
workout records** were re-read consistently and compared with their pre-run
contents: **zero changes**.

Both deployed API and worker Lambda packages were inspected read-only and still
contain the old workout writer, without history/locator maintenance. No backend
or frontend deployment was performed as part of this backfill request. This run
covers the existing retained sessions; repeat the backfill after deploying the
new writer to capture any workouts recorded in the intervening period. This
follow-up execution supersedes the earlier unverified inventory status above.
