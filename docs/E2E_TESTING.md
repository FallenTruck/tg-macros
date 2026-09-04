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

The live smoke targets only this account and may be reset again afterward.

## Run live checks

The live tests obtain the Mini App URL from CloudFormation and fetch both SSM
parameters at runtime. They use one in-memory Playwright browser context per
run; no `storageState` file, trace, video, or HAR is enabled.

```bash
make e2e-smoke
```

The smoke test covers browser login, generic bad-password rejection, refresh
session persistence, logout, Home/Profile/Workout navigation, responsive
overflow checks at 390 px, 360 px, and desktop width, and a PULL workout that:

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
