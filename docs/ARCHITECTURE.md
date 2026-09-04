# JavaanFitness deployment architecture

This document describes the current `tg-macros` application as deployed by the
`tg-macros-dev` stack. The repository is the source of truth for infrastructure
and application code; the deployed stack is the source of truth for the current
resource identifiers and outputs.

## System overview

```text
Telegram
   |
   v
API Gateway HTTP API
   |
   v
Webhook Lambda
   |
   v
SQS FIFO
   |
   v
Worker Lambda
   |
   +--> Telegram Bot API
   +--> OpenAI
   +--> DynamoDB


Telegram Mini App
       |
       v
   CloudFront
     /     \
    /       \
  /*       /api/*
   |          |
   v          v
Private S3  API Gateway
                |
                v
             API Lambda
                |
                v
             DynamoDB
```

Telegram webhook traffic goes directly to API Gateway and then to the webhook
processing path. Mini App browser traffic goes to CloudFront: `/api/*` is routed
to API Gateway and static paths are routed to the private S3 origin. API Gateway
is not upstream of CloudFront. CloudFront uses an Origin Access Control (OAC),
so the S3 bucket is not public. The same Mini App supports Telegram and
standalone browser authentication without a second frontend or API.

## Infrastructure source of truth

`template.yaml` is the SAM/CloudFormation source of truth. `samconfig.toml`
contains the default deployment configuration for the development stack:

| Setting | Current value |
| --- | --- |
| Stack | `tg-macros-dev` |
| Region | `ap-southeast-1` |
| Environment | `dev` |
| Lambda runtime | Python 3.13 |
| Lambda architecture | arm64 |
| Secret references | SSM parameter names, not secret values |
| Mini App URL parameter | `https://d1tav2v1lpusva.cloudfront.net` |

Do not create or repair these resources manually in AWS. Change the template,
parameters, or application code and deploy through SAM when an infrastructure
change is required.

## AWS resources

The development stack currently contains these logical groups:

| Logical resources | Responsibility |
| --- | --- |
| `HttpApi` | API Gateway HTTP API and `$default` stage |
| `WebhookFunction` | Validates the Telegram webhook secret and enqueues updates |
| `TelegramEventsQueue` / `TelegramEventsDLQ` | Ordered FIFO processing and failed-message retention |
| `WorkerFunction` | Processes Telegram updates and scheduled meal-action expiry |
| `ApiFunction` | Authenticated Mini App API, including workout endpoints |
| `FitnessDataTable` | Nutrition, profiles, programme data, and user-owned workout sessions |
| `IdempotencyTable` | Short-lived processing leases and duplicate-update protection |
| `MiniAppBucket` | Versioned private static-file storage |
| `MiniAppDistribution` | HTTPS CDN for the Mini App and API proxy path |
| `MiniAppOriginAccessControl` / `MiniAppBucketPolicy` | CloudFront-only S3 access |
| `*LogGroup` | Lambda logs with 14-day retention |
| `*Alarm` | Lambda, API Gateway, SQS age, DLQ, and throttle monitoring |

Observed current development outputs include:

- Mini App: `https://d1tav2v1lpusva.cloudfront.net`
- HTTP API: `https://n6aoev85e5.execute-api.ap-southeast-1.amazonaws.com`
- Webhook: `https://n6aoev85e5.execute-api.ap-southeast-1.amazonaws.com/telegram/webhook`
- Mini App bucket: `tg-macros-dev-miniappbucket-arodfjzchaq7`
- CloudFront distribution: `ELG11TWVMGNJZ`

These identifiers can change if the stack is replaced. Deployment scripts must
discover them from CloudFormation outputs/resources rather than hard-code them.

## Request and processing flows

### Telegram update

1. Telegram posts an update to API Gateway's `/telegram/webhook` route.
2. `lambda_handlers/webhook.py` loads the webhook secret from the configured
   SSM parameter, validates the request header, and parses the update.
3. The webhook Lambda sends a compact, versioned message to the FIFO queue,
   using the chat as the message group where available.
4. `lambda_handlers/worker.py` consumes one message at a time per configured
   batch item, obtains a DynamoDB idempotency lease, and performs the Telegram,
   nutrition, workout, or OpenAI workflow.
5. Failed messages are retried by SQS and eventually moved to the FIFO DLQ.

The webhook is intentionally thin. Business processing belongs in the worker
and the `macro_bot` service/data modules.

### Mini App request

1. The browser loads `index.html`, `app.js`, and `styles.css` from CloudFront.
2. `miniapp/app.js` sends API calls through the same CloudFront hostname.
3. In Telegram, non-empty `initData` is validated in `lambda_handlers/api.py`
   using the `X-Telegram-Init-Data` header. Invalid non-empty data is rejected
   and never downgraded to browser-session authentication.
4. Outside Telegram, the app calls `GET /api/auth/session`. If there is no
   valid `jf_session` cookie, it shows the browser-only login form. `POST
   /api/auth/login` verifies a known username/password and sets the cookie;
   `POST /api/auth/logout` revokes the server-side session and expires it.
5. Both paths resolve to the same `ServerlessIdentity`, and the API delegates
   to `macro_bot.serverless_service.NutritionService`. Domain services do not
   branch on the authentication method.
6. Mutating responses are marked `no-store`; the API origin request policy
   forwards only the `jf_session` cookie, the Telegram auth header, content
   type, accept, Origin, Referer, and query strings. The managed
   CachingDisabled cache policy remains in use for `/api/*`.

### Browser credentials and sessions

Browser access is restricted to credentials provisioned for known users. For
normal users, the Telegram identity remains canonical: provisioning takes an
existing Telegram user id, reads its existing identity record, and stores the
resulting internal `user_id` in the web credential record. The dev-only E2E
harness is the explicit exception: it creates a marked synthetic identity in
the separate `IDENTITY#E2E#javaan-e2e` namespace with no Telegram account. A
browser login never creates an unmarked application user or copies profile,
meal, target, programme, or workout data.

The retained `FitnessDataTable` uses these isolated single-table records:

| Entity | Key shape | Retention |
| --- | --- | --- |
| Web credential | `PK=WEB_CREDENTIAL#<sha256(username)>`, `SK=META` | Permanent; no `expires_at` |
| Browser session | `PK=WEB_SESSION#<sha256(opaque token)>`, `SK=META` | 30 days via `expires_at` TTL |

Credential records contain only the normalized username, canonical
`user_id`/Telegram id mapping, and versioned PBKDF2-HMAC-SHA256 material: a
random salt, iteration count, algorithm/version, and derived hash. Passwords
are never stored or logged. Session tokens are generated with cryptographic
randomness, are sent only in an `HttpOnly; Secure; SameSite=Strict; Path=/`
cookie, and are represented server-side by their hash. Logout deletes the
session record. A valid session re-reads the canonical Telegram identity and
requires its stored internal `user_id` to match the session mapping.

### Workout flow

`macro_bot/workout_programme.py` contains the shared immutable programme
definitions. `macro_bot/workout_execution.py` owns independent, durable user
executions. The API exposes programme reads and session operations including:

- `POST /api/workout/sessions`
- `GET /api/workout/sessions/active`
- `GET /api/workout/sessions/{session_id}`
- exercise substitution, exercise skip/reset, and set save/skip operations
- `POST /api/workout/sessions/{session_id}/cancel`
- `POST /api/workout/sessions/{session_id}/complete`

The Mini App switches between programme and active-workout views in state; it
does not add a frontend router. Starting or resuming an active session switches
to the active view and scrolls to that session. The active pointer remains in
DynamoDB, so reopening the Workout tab can find an unfinished session. Viewing
the programme does not cancel the session.

## Data ownership and retention

`FitnessDataTable` is an on-demand, encrypted DynamoDB table with point-in-time
recovery and a retained deletion/update-replacement policy. It uses string
partition and sort keys, with `expires_at` as the TTL attribute. The table holds
both shared programme records and user-owned nutrition/profile/workout records;
the service layer separates those keyspaces.

`IdempotencyTable` is a separate on-demand, encrypted, point-in-time-recoverable
table with the same retained policy and TTL mechanism. It protects Telegram
processing from duplicate delivery and stores short-lived leases/results; it is
not the workout history store.

The FIFO event queue retains messages for four days and moves messages to a
14-day FIFO DLQ after five receives. S3 versioning is enabled for Mini App
assets. CloudWatch Lambda log groups retain logs for 14 days.

## Security boundaries

- SSM Parameter Store holds the Bot API token, OpenAI key, and webhook secret.
  Code receives parameter names through environment variables and fetches values
  with decryption at runtime.
- Current SSM parameter names are:
  - `/tg-macros/prod/BOT_TOKEN`
  - `/tg-macros/prod/OPENAI_API_KEY`
  - `/tg-macros/prod/TELEGRAM_WEBHOOK_SECRET`
  These are parameter names only. Secret values must never be committed or
  printed.
- No secret values belong in Git, deployment output, or operational reports.
- The Mini App bucket blocks public access and accepts reads only from the
  CloudFront distribution through its OAC policy.
- API requests require valid Telegram launch/init data or a valid browser
  session and use user-scoped records. A Telegram user id by itself is never
  accepted as proof of identity.
- Browser cookie mutations require the configured `MINI_APP_URL` Origin or
  Referer. `SameSite=Strict` and same-origin fetches provide the normal browser
  boundary; Telegram init-data mutations are unaffected by this browser CSRF
  check.
- Login failures are generic and logged only with short fingerprints. The
  application deliberately does not add a stateful per-IP rate-limit service
  for this private two-user deployment; API Gateway/CloudFront controls and
  operational log monitoring remain the abuse-protection boundary.
- Logs use user fingerprints where telemetry needs correlation and avoid raw
  identifiers, payloads, and secrets.

## Application/runtime layout

The canonical application modules live in `macro_bot/` and the Lambda entry
points live in `lambda_handlers/`. `make sync-runtime` copies the canonical
modules, `food_catalog.json`, and the API/worker entry points into the runtime
bundle directories consumed by `template.yaml`:

- `lambda_handlers/runtime/` for API and worker code
- `lambda_handlers/webhook_runtime/` for the webhook entry point

Those directories are checked against their canonical sources by
`tests/test_runtime_bundle.py`. `.samignore` excludes tests, local tools,
repository data, and other non-runtime files from SAM packaging. `.aws-sam/`
is generated build output and is ignored by Git.
