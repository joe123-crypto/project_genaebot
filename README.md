# OpenClaw Local Deployment Source

This repository is the developer-owned layer around a local OpenClaw install.

It does not own or patch the installed OpenClaw npm package under
`/home/joseph/.npm-global/lib/node_modules/openclaw`. OpenClaw updates should
replace only package-managed files, while this repository tracks custom plugins,
commands, skills metadata, deployment helpers, and sanitized config templates.

Git commits and pushes are manual. Pull requests and `main` pushes run portable
structure, documentation-contract, syntax, unit, dependency, secret, Compose,
and browser-image checks on GitHub-hosted runners. A successful `main` push then
uses the self-hosted runner on this host to deploy the exact validated commit.

## Working Checkout and Container Boundary

This repository is checked out in two places on the host:

- `/home/joseph/.openclaw/` — the original checkout and OpenClaw's runtime home.
  The host-native gateway here is **retired** (its systemd user unit is disabled),
  so this tree's runtime data is now **stale**. Do not edit data here.
- `/home/joseph/projects/genaebot/` — **the editable source of truth.** Make code
  and config edits here and push to `main`; this is also where live data is edited
  (see below). Substitute this path for `/home/joseph/.openclaw` in the `cd`
  commands throughout this document.

The live gateway runs in the `openclaw-gateway` Docker container under a
read/write **boundary** (`deploy/docker-compose.override.yml`, installed):

- `workspace/` is mounted **read-only** from the staged `live-source` — code and
  context the container cannot modify; it changes only via deploy.
- `runtime/`, `agent-workspace-admin/`, and `agent-workspace-public/` are host
  bind-mounts from `/home/joseph/projects/genaebot/`. Runtime state remains live
  in one place. Agent context files are mounted individually from staged,
  source-controlled templates so the gateway cannot rewrite them.
- OpenClaw routes administrator DMs to the `main` agent in
  `agent-workspace-admin/` and other WhatsApp DMs to the restricted `public`
  agent in `agent-workspace-public/`. The public agent has no `MEMORY.md` or
  `COMMANDS.md` and is limited to the sender-bound `public_service` tool.
- credentials, identity, state, and `openclaw.json` stay in the base
  `openclaw-state` Docker volume.
- The override also mounts the current `entrypoint.sh` over the image's baked one,
  because the baked entrypoint blanket-`chown`s the whole tree and crashes on the
  read-only `workspace` mount.
- URL-only Job Scout applications run in the separate non-root
  `job-scout-browser` Playwright container. It has no published port, receives CVs
  through a read-only runtime mount, and returns only submitted/action-required/
  failed outcomes to the dispatcher. The worker uses Docker's default seccomp
  profile, drops all capabilities, keeps its root filesystem read-only, and has
  only `/tmp` writable. On this snap-Docker host, `no-new-privileges` prevents
  even the image entrypoint from executing, so Chromium readiness is enforced by
  the deep `/ready` probe instead.

Snap Docker can read these bind sources only because `/home/joseph/projects/...` is
non-hidden; the hidden `/home/joseph/.openclaw` cannot be bind-mounted under snap
confinement, which is why the editable checkout lives under `projects/`.

## Structure

```text
/home/joseph/.openclaw/
├── .git/                         # Canonical Git repo for this custom layer
├── .gitignore                    # Default-deny tracking policy
├── config/
│   └── openclaw.template.json    # Sanitized stable config patch/template
├── deploy/
│   ├── openclaw-deploy.sh        # Deterministic deploy script
│   ├── validate-openclaw-deploy.sh
│   ├── ci-check.sh                # Portable repository validation and tests
│   ├── ci/                        # Structure/docs policy and mappings
│   ├── stage-openclaw-source.sh   # Shared CI/production staging helper
│   ├── stage-workspace.rsync      # Single deployable-workspace allowlist
│   ├── check-openclaw-mirror.sh  # Generated mirror freshness/runtime check
│   ├── patch-openclaw-config.py
│   ├── github-actions/           # Source-controlled host automation helpers
│   ├── docker-compose.override.yml
│   ├── agent-workspace-admin/    # Private administrator context template
│   ├── agent-workspace-public/   # Restricted public context template
│   └── container/entrypoint.sh   # Tracked custom container entrypoint
├── workspace/                    # Developer-owned source/context
├── agent-workspace-admin/        # Live private admin workspace, ignored
├── agent-workspace-public/       # Live restricted public workspace, ignored
├── runtime/                      # Command/runtime state, ignored
└── gitclaw/                      # Retired GitClaw archive, ignored
```

`workspace/` contains source and context: plugins, command wrappers, scripts,
docs, selected skills, and agent identity files. It is not the public bot's active
working room anymore.

`agent-workspace-admin/` is the `main` agent's private room and contains operator
memory and context. `agent-workspace-public/` is the public agent's room; its
bootstrap files contain public-only context and are mounted read-only. OpenClaw
may write its small workspace-state file there, but the public model has no
general shell or filesystem tools.

`runtime/` is for mutable command state such as account-link caches, public-news
subscriptions, public-service routing state, cron state, and cron logs. It is
volume data, not source.

## Git Tracking

The repository uses a default-deny `.gitignore`: nothing is tracked unless it is
explicitly allowed.

Tracked categories:

- `config/openclaw.template.json`
- `deploy/**`
- `workspace/*.md`
- `workspace/bin/**`
- `workspace/lib/*.py`
- `workspace/plugins/**`
- `workspace/cron/**/*.py` and `workspace/cron/**/*.sh`
- `workspace/docs/**`
- custom source folders such as `account-link/`, `public-news/`,
  `webetu-preferences/`, and `job-scout/` (including its `scrapers/`,
  `jobscout/`, and `channel-listener/` helper packages). Each service folder is
  staged whole by a per-service recursive glob in
  `deploy/stage-workspace.rsync` (e.g. `+ /job-scout/***`), so adding or
  splitting an internal module ships automatically without editing the deploy
  allowlist; the global runtime/state/cache excludes at the top of that filter
  still keep `__pycache__/`, `node_modules/`, `*.log`, and `*.json` state files
  out of the deployable source.
- selected custom skills plus `workspace/skills.lock.json`

Ignored categories:

- `.env`, `gateway.systemd.env`, and all secrets
- the live `openclaw.json` (no longer tracked; generated from
  `config/openclaw.template.json`) and its backup/pre-update variants such as
  `openclaw.json.last-good` and `openclaw.json.pre-update*`
- credentials, devices, identity, sessions, memory DBs, logs, runtime DBs
- `npm/`, media, browser data, caches, Python bytecode, virtualenvs
- generated job/CV artifacts and runtime state files
- the retired `gitclaw/` archive

Before upload, inspect exactly what Git sees:

```bash
cd /home/joseph/.openclaw
git status --short
git ls-files --others --exclude-standard
```

## Deploying Changes

Normal production deploy path:

```bash
cd /home/joseph/.openclaw
git status --short
git commit -m "Describe the change"
git push origin main
```

GitHub Actions first validates the commit on hosted runners. The production job
starts only after portable validation, secret scanning, dependency checks, and
the conditional Job Scout browser build succeed. It then checks out the same
commit on the `openclaw-prod` self-hosted runner and runs:

```bash
sudo /usr/local/sbin/openclaw-prod-deploy "$GITHUB_WORKSPACE"
```

The sudo rule is intentionally narrow: the runner user may run only that wrapper,
not arbitrary root commands. The wrapper refuses to deploy anything except a clean
checkout of `joe123-crypto/project_genaebot` on `main`, and it uses a lock so two
deploys cannot overlap. Repository fetches and revision checks run as the checkout
owner `joseph`, so both Actions' temporary credentials and Joseph's normal HTTPS
credential helper work without giving GitHub credentials to root. Before dependency installation or deployment, the workflow
and wrapper run `deploy/check-deploy-resources.sh`. The preflight requires at least
1 GiB of immediately available memory and 6 GiB of combined available memory plus
free swap, and reports the largest processes without changing production if the
host is under pressure.

Cross-repository Job Scout releases use a second hosted validation gate in the
same workflow. Before enabling that event, install the reviewed request-aware
root wrapper once:

```bash
cd /home/joseph/.openclaw
sudo ./deploy/github-actions/install-openclaw-prod-deploy-wrapper.sh
sudo cmp deploy/github-actions/openclaw-prod-deploy /usr/local/sbin/openclaw-prod-deploy
```

The self-hosted job cannot accept UI-entered artifact digests. It receives a
hosted-validated request and bundle, hashes the request, and passes the path and
hash through the narrow root wrapper. Manual Job Scout rollback selects only an
unexpired, previously successful production ledger by manifest hash and must
also match the root-owned, hash-chained successful-release ledger on the host.
The ledger is deployment metadata under `/var/lib/openclaw-deploy`; rollback
does not read or modify Job Scout runtime state. The full
payload, GHCR access, validation, and rollback contracts are documented in
`deploy/github-actions/README.md`.

Validate without deploying:

```bash
cd /home/joseph/.openclaw
./deploy/ci-check.sh
./deploy/validate-openclaw-deploy.sh
```

`ci-check.sh` is host-independent. The deployment validator adds host-only
checks for retired GitClaw automation, ignored private paths, and generated
mirror state. CI also applies `deploy/ci/docs-contract.json`: changes to a
declared public, onboarding, or operations interface must update the mapped
documentation in the same pull request or push.

The repository is currently private on a GitHub plan that does not provide
branch protection. A failing commit can therefore reach `main`, but the workflow
will not deploy it. Production rollback is performed by reverting the bad commit
on `main`; the revert passes through the same validation and deployment path.

Manual fallback deploy for emergency/local use:

```bash
cd /home/joseph/projects/genaebot
sudo /usr/local/sbin/openclaw-prod-deploy "$PWD"
```

Job Scout scheduler activation is deliberately separate from a normal gateway
deploy. Use this order:

1. Add these private values to `/home/joseph/openclaw-deploy/.env`:

```text
JOB_SCOUT_BROWSER_TOKEN=<at least 32 random bytes>
```

   Do not put this value in Git. The same Compose interpolation supplies the
   browser token to the gateway and worker, preventing mismatched delivery paths.

2. Deploy the Agent Genaie API schema change, then deploy this repository. The
   core gateway starts independently. The optional browser is built afterward;
   failure leaves Job Scout degraded but does not stop WhatsApp or other services.

3. CVs now live in the Agent Genaie Cloudflare R2 bucket (object key
   `<uid>/cv/cv.pdf`), and `cvFileRef` in Firestore holds that key. Existing
   on-disk CVs were already migrated into R2 by the Agent Genaie
   `scripts/migrate-cvs-to-r2.ts` one-off; the genaebot `migrate-runtime-cvs`
   command has been removed. New CVs are uploaded to R2 by `stage-cv` /
   `save-profile`, and the worker downloads a presigned URL at dispatch time.
   The gateway converts uploaded PDFs into a structured A4 HTML template using
   Poppler and Tesseract OCR, with plain text extraction as a fallback. The
   conversion removes browser print headers, local `file://` paths, timestamps,
   and page counters before any personalized CV is rendered.

4. Any ready user without a CV in R2 must upload it again through the normal Job
   Scout profile flow. Local files under `runtime/job-scout/users` are disposable
   caches and are not evidence that an R2 CV exists or is missing.

5. Install or update the schedule:

```bash
sudo ./deploy/install-job-scout.sh
```

The installer refuses to enable the cron unless the worker is healthy, the
gateway and worker tokens match, at least one subscriber is ready, and every
ready CV is accessible. It installs an isolated `main` agent turn at
`08:00 Africa/Harare` with a six-hour run budget. The parent prepares every
ready subscriber and delegates four isolated same-model children at a time;
children score the deterministic shortlist, while the validated dispatcher
remains the only component allowed to record, submit, or send. For subscribers
with `autoApply: false`, strong email and website matches are recorded as
`pending_approval` without creating personalized documents or submitting an
application. The dashboard exposes those records for approval. A separate
five-minute `job-scout-approvals` worker picks up records the user changed to
`approved`, then runs CV and application-letter customization before the
dispatcher sends email applications or submits supported website forms.

### Dashboard task telemetry

After each per-user run, the Job Scout dispatch and the Webetu meals notifier
publish a live task snapshot to Agent Genaie's `POST /internal/dashboard/tasks`
(rendered by the signed-in dashboard) through
`workspace/lib/dashboard_telemetry.py`. Publishing is best-effort: failures log
one stderr warning and never change the run outcome. It reuses `APP_BASE_URL`
and `AGENT_GENAI_INTERNAL_API_KEY` via `genaie_env`, so no extra configuration
is needed.

### WhatsApp Channel sources

Job Scout can also consume job adverts from the ZimLinks,
ZIM•JOB•VACANCIES, and EveryDayVacancies WhatsApp Channels. A
separate `job-scout-channel-listener` container uses its own linked-device auth;
never point it at OpenClaw's WhatsApp auth directory. It captures posts in real
time, backfills the latest 10 from each channel after pairing/reconnect, and keeps transient state
under `runtime/job-scout-channel/`. The daily Job Scout dispatch reads normalized
Zimbabwe candidates from that queue. Raw post text/media is removed after all
selected users reach terminal outcomes; application records remain in the
existing Agent Genaie database.

After a normal deploy, pair the isolated listener once:

```bash
./deploy/pair-job-scout-channel.sh
```

Scan the displayed QR code with the WhatsApp account that should follow the
configured channels. The script verifies every channel identity, starts the listener, and
installs a 15-minute health check. Media-only posts are discarded because this
version deliberately does not perform OCR.

The deploy script used by both paths:

- verifies GitClaw autocommit is disabled
- shows Git status and diff stats
- rejects staged runtime/secret files
- runs syntax and JSON checks
- stages curated source into `/home/joseph/openclaw-deploy/live-source` using
  the same helper and allowlist exercised by hosted CI
- installs the source-controlled gateway Dockerfile, including the
  `poppler-utils` and `tesseract-ocr` dependencies used for structured Job
  Scout CV conversion
- preserves the `live-source` root inode so Docker's read-only bind mount cannot
  become detached from the newly staged files
- stamps `live-source` with `.OPENCLAW_GENERATED` and a source manifest
- compares the staged-source and container-boundary digest with the last applied
  digest; an unchanged deploy skips the Compose build and preserves the running
  gateway process
- excludes runtime files such as `state.json`, `pending-links.json`,
  `subscriptions.json`, `*.log`, caches, and bytecode, and removes stale
  excluded files from the existing generated mirror during restaging
- syncs curated source into the live container (skipped when the container
  boundary is active — `workspace/` and agent-room docs then arrive via the
  read-only bind mounts instead)
- applies `config/openclaw.template.json` to the container's live config only when
  deploy inputs changed
- verifies the `main` and `public` agent definitions, their separate workspace
  paths, required context files, and the absence of public `MEMORY.md` and
  `COMMANDS.md`
  with bounded retries while a changed container stabilizes
- reconciles `openclaw-gateway-container.service` without forcing container
  recreation; unchanged inputs do not restart the gateway
- installs and runs the source-controlled deployment health check; when Webetu is
  disabled it does not contact the worker or Firestore, and quota exhaustion from
  an enabled Webetu probe is an isolated warning rather than a gateway failure
- waits for two consecutive authenticated gateway API status probes before any
  cron reconciliation, preventing transient WebSocket startup closures from
  interrupting deployment after a successful container restart
- reconciles the daily Job Scout schedule to the source-controlled isolated
  agent turn at 08:00 `Africa/Harare`; an existing deliberate pause is preserved
  as disabled, so deployment cannot silently re-enable applications
- best-effort builds and readiness-checks the optional Job Scout browser after
  the core gateway is healthy; failure is reported without failing the gateway
  deployment
- installs or reconciles the isolated `job-scout-approvals` worker, which checks
  every five minutes for dashboard-approved applications and processes them
  under the user's approval
- reconciles the main Job Scout schedule only after its subscriber/CV readiness
  gate passes; Agent Genaie Firestore quota exhaustion defers reconciliation and
  preserves the existing schedule without failing the gateway deploy, while all
  other readiness failures remain fatal
- writes `/home/joseph/openclaw-deploy/.last-successful-deploy.json` only after all
  required readiness, health, and reconciliation steps finish successfully

Host automation files:

```text
.github/workflows/deploy-main.yml
deploy/github-actions/openclaw-prod-deploy
deploy/github-actions/register-openclaw-runner.sh
deploy/github-actions/install-openclaw-runner-hardening.sh
deploy/github-actions/ensure-openclaw-swap.sh
deploy/systemd/actions-runner-openclaw-prod.override.conf
/usr/local/sbin/openclaw-prod-deploy
/etc/sudoers.d/openclaw-prod-deploy
/swap-openclaw.img
/home/joseph/actions-runner-openclaw-prod/
```

The runner override restarts the service after failures and keeps OOM handling
within its control group. `ensure-openclaw-swap.sh` provisions a dedicated,
persistent 8 GiB swap file at the exact path above and refuses unexpected targets
or sizes.

If the runner ever needs to be registered again, get a fresh registration token
from the GitHub repository settings and run:

```bash
cd /home/joseph/.openclaw
sudo RUNNER_TOKEN='...' ./deploy/github-actions/register-openclaw-runner.sh
```

The container boundary is now installed and is the standard runtime. Normal
deploys do **not** pass `--apply-container-boundary`; the deploy detects the
installed override and leaves it in place (and skips the in-container source sync,
which the read-only bind mounts make redundant). The flag is only needed to
(re)install the override from scratch.

## Deploy Mirror

`/home/joseph/openclaw-deploy/live-source` is generated output, not an editable
source tree. It can be stale between deploys. If a file differs between
`.openclaw/workspace` and `openclaw-deploy/live-source`, the canonical version is
the one under `.openclaw`.

Check mirror state:

```bash
cd /home/joseph/.openclaw
./deploy/check-openclaw-mirror.sh
```

Refresh the mirror and live container:

```bash
cd /home/joseph/.openclaw
sudo ./deploy/openclaw-deploy.sh
```

The old manual sync workflow has been archived under:

```text
/home/joseph/openclaw-deploy/archive/manual-sync-20260618T182611/
```

Do not use archived `live-sync-stage` or `apply-*.sh` files for live changes.

## Config Changes

`openclaw.json` lives in the base `openclaw-state` Docker volume (it is not
bind-mounted), so editing a host copy does not update the live container.

For stable deterministic config changes, edit:

```text
config/openclaw.template.json
```

Then deploy:

```bash
sudo ./deploy/openclaw-deploy.sh
```

`openclaw.json` is no longer tracked (it is gitignored). The tracked source of
truth for config is `config/openclaw.template.json`, which the deploy merges into
the live `openclaw.json` via `patch-openclaw-config.py`. The backup and
pre-update variants remain ignored.

The public WhatsApp system prompt is tracked separately at
`workspace/docs/public-whatsapp-system-prompt.txt`. Every deployment injects it
into `channels.whatsapp.direct["*"].systemPrompt` while preserving runtime user
data and peer-specific overrides. Reverting a prompt commit and redeploying
therefore restores the earlier prompt without a manual volume edit.

## Operational Changes

Runtime/user operations should go through approved commands instead of hand-editing
config files.

Inside the live container, use:

```bash
sudo /snap/bin/docker exec -u joseph openclaw-gateway \
  /home/joseph/.openclaw/workspace/bin/openclaw-cap --help
```

Examples:

```bash
sudo /snap/bin/docker exec -u joseph openclaw-gateway \
  /home/joseph/.openclaw/workspace/bin/openclaw-cap users list

sudo /snap/bin/docker exec -u joseph openclaw-gateway \
  /home/joseph/.openclaw/workspace/bin/openclaw-cap account-link status --phone +213...

sudo /snap/bin/docker exec -u joseph openclaw-gateway \
  /home/joseph/.openclaw/workspace/bin/openclaw-cap cron run webetu-meals --dry-run

  # Inspect private pending summaries without exposing phone numbers or message bodies.
  /home/joseph/.openclaw/workspace/bin/openclaw-cap cron run webetu-meals --list-failed

  # Explicitly retry one audited delivery; retries are never automatic.
  /home/joseph/.openclaw/workspace/bin/openclaw-cap cron run webetu-meals --retry-failed <delivery-id>
```

Rule of thumb:

- Source/plugin/script/template changes require deploy.
- Runtime user operations should use `openclaw-cap`.
- Runtime state under `runtime/` is preserved by deploys and should not be
  committed.
- Git commits and pushes are always manual.

## Runtime Volume Data

Mutable service data now lives under:

```text
runtime/
├── account-link/pending-links.json
├── public-news/subscriptions.json
├── public-service-gate/invites.json (member store; invite codes retired)
├── public-service-gate/state.json
└── cron-state/
    ├── algeria-exhibitions-listener/state.json
    ├── github-issue-tracker/state.json
    └── webetu-meals/notification.log
```

These files are ignored by Git and excluded from deployment staging. Under the
container boundary, `runtime/` is a host bind-mount from this checkout, so editing
a file here (e.g. `runtime/public-service-gate/invites.json`) is the live edit the
container sees immediately. Treat them as live data: back them up or inspect them
directly, but do not upload them as source.

The deployable source list is controlled by `deploy/openclaw-deploy.sh` in
`stage_source()`. To see what the last deploy staged, inspect:

```bash
find /home/joseph/openclaw-deploy/live-source -type f | sort
```

To verify that generated mirror is fresh and clean:

```bash
./deploy/check-openclaw-mirror.sh
```

To see what Git can upload, use:

```bash
git status --short
git ls-files
```

## OpenClaw Updates

This repo must not edit package-managed files under:

```text
/home/joseph/.npm-global/lib/node_modules/openclaw
```

After `openclaw update`, run:

```bash
cd /home/joseph/.openclaw
./deploy/validate-openclaw-deploy.sh
```

If validation passes, the custom deployment layer is still intact.
