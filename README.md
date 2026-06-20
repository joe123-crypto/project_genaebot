# OpenClaw Local Deployment Source

This repository is the developer-owned layer around a local OpenClaw install.

It does not own or patch the installed OpenClaw npm package under
`/home/joseph/.npm-global/lib/node_modules/openclaw`. OpenClaw updates should
replace only package-managed files, while this repository tracks custom plugins,
commands, skills metadata, deployment helpers, and sanitized config templates.

Git commits and pushes are manual, but production deploys are automated from
GitHub: when `main` updates, the self-hosted GitHub Actions runner on this host
validates the pushed commit and deploys it to the live container.

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
- `runtime/` and `agent-workspace/` are **host bind-mounts from
  `/home/joseph/projects/genaebot/`** — editable in one place and live to the
  container immediately (e.g. `runtime/public-service-gate/invites.json`). There is
  no separate "host copy" to keep in sync.
- credentials, identity, state, and `openclaw.json` stay in the base
  `openclaw-state` Docker volume.
- The override also mounts the current `entrypoint.sh` over the image's baked one,
  because the baked entrypoint blanket-`chown`s the whole tree and crashes on the
  read-only `workspace` mount.

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
│   ├── check-openclaw-mirror.sh  # Generated mirror freshness/runtime check
│   ├── patch-openclaw-config.py
│   ├── github-actions/           # Source-controlled host automation helpers
│   ├── docker-compose.override.yml
│   ├── agent-workspace/          # Files copied into the live agent workspace
│   └── container/entrypoint.sh   # Tracked custom container entrypoint
├── workspace/                    # Developer-owned source/context
├── agent-workspace/              # Small writable agent workspace, ignored
├── runtime/                      # Command/runtime state, ignored
└── gitclaw/                      # Retired GitClaw archive, ignored
```

`workspace/` contains source and context: plugins, command wrappers, scripts,
docs, selected skills, and agent identity files. It is not the public bot's active
working room anymore.

`agent-workspace/` is the public bot's active room in the live container. It is
small by design: read-only context files plus writable `data/` and `files/`.

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
  `webetu-preferences/`, and `job-scout/`
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

GitHub Actions then runs on this host through the `openclaw-prod` self-hosted
runner. The workflow checks out the exact pushed commit, validates it, and runs:

```bash
sudo /usr/local/sbin/openclaw-prod-deploy "$GITHUB_WORKSPACE"
```

The sudo rule is intentionally narrow: the runner user may run only that wrapper,
not arbitrary root commands. The wrapper refuses to deploy anything except a clean
checkout of `joe123-crypto/project_genaebot` on `main`, and it uses a lock so two
deploys cannot overlap.

Validate without deploying:

```bash
cd /home/joseph/.openclaw
./deploy/validate-openclaw-deploy.sh
```

Manual fallback deploy for emergency/local use:

```bash
cd /home/joseph/.openclaw
sudo ./deploy/openclaw-deploy.sh
```

The deploy script used by both paths:

- verifies GitClaw autocommit is disabled
- shows Git status and diff stats
- rejects staged runtime/secret files
- runs syntax and JSON checks
- stages curated source into `/home/joseph/openclaw-deploy/live-source`
- stamps `live-source` with `.OPENCLAW_GENERATED` and a source manifest
- excludes runtime files such as `state.json`, `pending-links.json`,
  `subscriptions.json`, `*.log`, caches, and bytecode
- syncs curated source into the live container (skipped when the container
  boundary is active — `workspace/` and agent-room docs then arrive via the
  read-only bind mounts instead)
- applies `config/openclaw.template.json` to the container's live config
- verifies `agents.defaults.workspace=/home/joseph/.openclaw/agent-workspace`
- restarts `openclaw-gateway-container.service`
- runs the deployment health check

Host automation files:

```text
.github/workflows/deploy-main.yml
deploy/github-actions/openclaw-prod-deploy
deploy/github-actions/register-openclaw-runner.sh
/usr/local/sbin/openclaw-prod-deploy
/etc/sudoers.d/openclaw-prod-deploy
/home/joseph/actions-runner-openclaw-prod/
```

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
├── public-service-gate/invites.json
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
