# JavaanFitness deployment runbook

This runbook covers the existing `tg-macros-dev` stack and Mini App. It does
not create infrastructure manually. A Git push updates the repository only;
it does not deploy Lambda code or static Mini App assets.

## Deployment boundaries

| Change | Command | AWS effect |
| --- | --- | --- |
| Runtime source mirror | `make sync-runtime` | Local generated runtime source only |
| Backend/infrastructure | `sam build --use-container` then `sam deploy` | CloudFormation/Lambda/API resources as required |
| Mini App frontend | `make deploy-miniapp` | S3 assets plus CloudFront invalidation |
| Full release | Backend sequence, then frontend sequence | Both release paths |

Browser authentication changes are a backend, frontend, and CloudFront
release. The CloudFront `/api/*` origin request policy forwards only the
`jf_session` cookie plus the allowlisted application/auth headers; it does not
forward unrelated browser cookies.

`sam deploy` does not upload the Mini App contents to the application bucket.
`make deploy-miniapp` does not deploy Lambda or CloudFormation changes.
Pushing to Git also does not deploy either release path.

## Provision a known browser login

Provisioning is an administrative local command, not a public registration
flow. It must target an already-existing Telegram-linked JavaanFitness user;
the command checks that identity before writing anything. Run it with the AWS
profile and table context for the intended stack:

```bash
AWS_PROFILE=fitness-dev AWS_REGION=ap-southeast-1 \
  .venv/bin/python scripts/provision_web_login.py \
  --username <known-browser-username> \
  --telegram-user-id <existing-telegram-user-id>
```

The command prompts for the password twice without echoing it, hashes it
locally with PBKDF2-HMAC-SHA256, and writes only the credential record to the
retained `FitnessDataTable`. It never accepts a password as a normal command
line argument. To reset the same known login, repeat the command with
`--replace`; it will not reassign the username to a different identity.
Do not put usernames, passwords, hashes, session tokens, or Telegram init data
in Git, deployment output, or reports.

| Change type | Tests | `make sync-runtime` | SAM build/deploy | Mini App deploy |
| --- | --- | --- | --- | --- |
| CSS / HTML only | Yes | No | No | Yes |
| Mini App JS only | Yes | No, unless the runtime copy changed | No | Yes |
| Backend Python | Yes | Yes | Yes | Only if frontend changed |
| API backend | Yes | Yes | Yes | Only if frontend changed |
| `template.yaml` / SAM infrastructure | Yes | As applicable | Yes | Normally no |
| Frontend + backend | Yes | Yes | Yes | Yes |

## Variables and prerequisites

The normal development context is:

```bash
export AWS_PROFILE=fitness-dev
export AWS_REGION=ap-southeast-1
export STACK_NAME=tg-macros-dev
```

Required local tools are Git, AWS CLI, AWS SAM CLI, Python, and Docker through
Colima. Authenticate with AWS SSO when needed:

```bash
aws sso login --profile "$AWS_PROFILE"
aws sts get-caller-identity --profile "$AWS_PROFILE" --region "$AWS_REGION"
```

The identity check is safe to record without printing credentials or parameter
values.

## Colima-backed SAM build

This Mac uses Colima's Docker daemon with the macOS Virtualization.framework,
Docker runtime, and arm64 Linux containers. Do not install Docker Desktop for
this project.

Check the backend before a containerized build:

```bash
colima status
docker context show
docker version
docker ps
```

For SAM, explicitly use the Colima socket and a clean Docker configuration. The
clean configuration avoids a stale Docker Desktop credential helper such as
`docker-credential-desktop`:

```bash
mkdir -p "$HOME/.docker-sam"
printf '{}\n' > "$HOME/.docker-sam/config.json"
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
export DOCKER_CONFIG="$HOME/.docker-sam"
unset DOCKER_TLS_VERIFY DOCKER_CERT_PATH
docker version
docker ps
```

The Docker server must report Linux/arm64. If it does not, stop and diagnose
the context/socket before running SAM. Do not kill broad process groups, move
the repository, or copy build trees to work around a discovery error.

## Backend deployment

Run the local preparation and validation first:

```bash
make sync-runtime
sam validate --lint
sam build --use-container
```

`samconfig.toml` supplies the stack name, region, managed SAM artifact bucket,
IAM capability, SSM parameter names, and Mini App URL. Deploy it with the same
AWS context:

```bash
AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$AWS_REGION" sam deploy
```

CloudFormation should finish in `UPDATE_COMPLETE`. Inspect status and outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].{Status:StackStatus,Outputs:Outputs}' \
  --output json
```

If deployment fails, inspect stack events and the change set before changing
anything. Do not manually recreate Lambda, API Gateway, S3, CloudFront,
DynamoDB, SQS, or IAM resources.

## Mini App deployment

The supported frontend command discovers `MiniAppBucketName`,
`MiniAppDistribution`, and `MiniAppUrl` from CloudFormation:

```bash
AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$AWS_REGION" make deploy-miniapp
```

`scripts/deploy_miniapp.sh` performs this sequence:

1. Reads the bucket from the `MiniAppBucketName` stack output.
2. Reads the distribution ID from the `MiniAppDistribution` stack resource.
3. Computes a content-derived 12-character SHA-256 version from the three
   source frontend files.
4. Stages `index.html` with `__MINIAPP_VERSION__` replaced in its
   `app.js` and `styles.css` URLs.
5. Uploads the root HTML with `no-cache, no-store, must-revalidate`.
6. Uploads root JavaScript/CSS with a one-year immutable cache policy.
7. Updates compatibility aliases under `miniapp/static/` without deleting
   unrelated bucket objects.
8. Invalidates the root assets and compatibility aliases, then waits for
   CloudFront invalidation completion.

This content-derived versioning is the normal cache-busting mechanism. Do not
use a broad `aws s3 sync --delete` against this bucket unless the bucket has
first been verified to contain only files owned by the current frontend release.

## Full release

Use this order when both backend and frontend changed:

```bash
export AWS_PROFILE=fitness-dev AWS_REGION=ap-southeast-1 STACK_NAME=tg-macros-dev
make sync-runtime
sam validate --lint
DOCKER_HOST="unix://$HOME/.colima/default/docker.sock" \
DOCKER_CONFIG="$HOME/.docker-sam" \
  sam build --use-container
AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$AWS_REGION" sam deploy
AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$AWS_REGION" make deploy-miniapp
```

Wait for the backend stack to complete before running the frontend helper, so
the helper discovers the final bucket, distribution, and URL.

## Validation and smoke checks

Before committing source changes:

```bash
git diff --check
make sync-runtime
sam validate --lint
node --check miniapp/app.js
.venv/bin/python -m compileall -q macro_bot lambda_handlers
.venv/bin/python -m unittest discover -s tests
git status --short
```

After a frontend deployment, verify CloudFront itself rather than only S3:

```bash
MINIAPP_URL="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='MiniAppUrl'].OutputValue | [0]" \
  --output text)"
curl -fsS "$MINIAPP_URL/"
curl -fsS "$MINIAPP_URL/app.js"
curl -fsS "$MINIAPP_URL/styles.css"
```

The HTML should reference the current versioned `app.js` and `styles.css`.
The CSS response should contain the active-workout responsive rules, including
`.workout-skip-controls`, `display: flex`, `flex-wrap: wrap`, and the select
sizing rule. The JavaScript response should contain the current `Save Set`,
`Skip Set`, active-workout mode, and workout completion UI.

After a backend deployment, check the API health route:

```bash
HTTP_API_URL="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='HttpApiUrl'].OutputValue | [0]" \
  --output text)"
curl -fsS "$HTTP_API_URL/api/health"
```

For a browser-auth smoke test through the CloudFront URL, use a private shell
or browser session and do not record credentials or cookies:

```text
Open the Mini App URL directly in Safari/Chrome.
Confirm the browser-only sign-in form appears without opening Telegram.
Sign in with a provisioned login and confirm the existing profile/target and
workout data are present. Reload the page to confirm the session persists.
Use Log out and confirm the login form returns; then confirm Telegram still
opens the app automatically with valid init data.
```

The browser session API is `POST /api/auth/login`, `GET /api/auth/session`,
and `POST /api/auth/logout`. Login and logout are same-origin protected; do not
paste their request bodies, cookies, or identity-bearing responses into logs or
reports.

For the isolated live browser harness and its dev-only synthetic account, read
[`docs/E2E_TESTING.md`](E2E_TESTING.md) before running `make e2e-*` commands.

Workout API calls require valid Telegram init data and a user-owned session.
With a test Mini App session, exercise the authenticated completion route:

```text
POST /api/workout/sessions/{session_id}/complete
X-Telegram-Init-Data: <valid init data>
body: {"expected_revision": <current revision>}
```

Do not put init data, bot tokens, SSM values, or signed request material in
logs or reports. The deployed route is implemented by `ApiFunction`; the local
API tests and `tests/test_runtime_bundle.py` verify that the completion code is
included in the Lambda runtime bundle.

## Backend-only and frontend-only releases

For backend-only changes, run `sync-runtime`, lint, the Colima-backed SAM build,
and `sam deploy`. Do not invalidate CloudFront unless static files changed.

For frontend-only changes, run the frontend tests and
`make deploy-miniapp`. Do not run `sam deploy` unless `template.yaml`, SAM
configuration, or backend/runtime source changed.

## Troubleshooting

### AWS authentication or permissions

If AWS reports an expired SSO session, run `aws sso login --profile
fitness-dev` and repeat the identity check. If access is denied, record the
specific operation and resource and ask for the missing permission; never work
around it by creating replacement resources.

### SAM cannot reach Docker

Confirm Colima is running, the Docker server is Linux/arm64, and the shell has
the explicit `DOCKER_HOST` and clean `DOCKER_CONFIG` above. Use
`sam build --use-container --debug` to identify whether the remaining failure is
daemon discovery, image pull, credentials, mounting, architecture, network,
permissions, or a Python dependency. Fix the concrete cause only.

### Stale frontend content

Run `make deploy-miniapp`, wait for its invalidation, and verify the CloudFront
responses. Because HTML is no-cache and JS/CSS URLs include a content version,
an old Mini App WebView may still need to be closed and reopened. Do not treat
Git history or S3 object presence alone as proof of a CloudFront release.

### CloudFormation failure

Inspect stack events with `aws cloudformation describe-stack-events`. Preserve
retained DynamoDB data and existing resources while diagnosing. Resolve the
template, parameter, IAM, quota, or dependency issue and redeploy through SAM.

## Rules for future changes

- Keep `template.yaml` and `samconfig.toml` as infrastructure sources of truth.
- Keep secrets in SSM and refer to parameter names only.
- Preserve DynamoDB retained policies and user-owned workout/history data.
- Keep `lambda_handlers/runtime/` synchronized from canonical sources.
- Treat `sam deploy` and Mini App S3/CloudFront deployment as separate actions.
- Discover bucket, distribution, and URLs from CloudFormation outputs/resources.
- Do not force-push, commit `.aws-sam/`, or commit generated/runtime data that is
  excluded by `.samignore`.
- Do not introduce a frontend framework or alter workout domain logic for a
  presentation-only change.
- Make the smallest safe change, validate it locally, then verify the deployed
  endpoint or CloudFront response.

### Nutrition Lab release

The Nutrition Lab is a backend, infrastructure, and Mini App release. The SAM
parameter `EnableE2ENutritionLab` defaults to false; the documented dev
`samconfig.toml` opts in. CloudFormation additionally requires the exact dev
stack name, environment and region before creating `NutritionLabImages`,
`NutritionLabFunction`, its log group, or the API's Lab permissions. Use the
normal full release sequence, then `make e2e-nutrition-lab`. The additional S3
bucket is private, encrypted, and contains only temporary test images with a
one-day expiration rule. Disabling/removing the feature may require waiting for
that bucket to empty before CloudFormation can delete it; do not delete retained
nutrition tables to resolve such an error.
