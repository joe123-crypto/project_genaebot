# OpenClaw Local Deployment Source

This repository is the developer-owned layer around a local OpenClaw install.

It does not own or patch the installed OpenClaw npm package under
`/home/joseph/.npm-global/lib/node_modules/openclaw`. OpenClaw updates should
replace only package-managed files, while this repository tracks custom plugins,
commands, skills metadata, deployment helpers, and sanitized config templates.

Git is manual: review changes, commit intentionally, and push to the chosen remote
yourself. No cron job or deploy script should auto-commit or auto-push.

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

- `openclaw.json`
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
- OpenClaw backup/pre-update files such as `openclaw.json.last-good` and
  `openclaw.json.pre-update*`
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

Validate without deploying:

```bash
cd /home/joseph/.openclaw
./deploy/validate-openclaw-deploy.sh
```

Deploy source/config-template changes to the live Docker container:

```bash
cd /home/joseph/.openclaw
sudo ./deploy/openclaw-deploy.sh
```

The deploy script:

- verifies GitClaw autocommit is disabled
- shows Git status and diff stats
- rejects staged runtime/secret files
- runs syntax and JSON checks
- stages curated source into `/home/joseph/openclaw-deploy/live-source`
- stamps `live-source` with `.OPENCLAW_GENERATED` and a source manifest
- excludes runtime files such as `state.json`, `pending-links.json`,
  `subscriptions.json`, `*.log`, caches, and bytecode
- syncs curated source into the live container volume
- copies agent-room docs into `agent-workspace/`
- applies `config/openclaw.template.json` to the container's live config
- verifies `agents.defaults.workspace=/home/joseph/.openclaw/agent-workspace`
- restarts `openclaw-gateway-container.service`
- runs the deployment health check

Do not use `--apply-container-boundary` during normal deploys. That flag installs
the stricter Docker Compose read-only/writable mount override and should only be
used when intentionally testing the container-boundary model.

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

The live gateway runs from Docker volumes. Editing the host copy of
`/home/joseph/.openclaw/openclaw.json` does not automatically update the live
container.

For stable deterministic config changes, edit:

```text
config/openclaw.template.json
```

Then deploy:

```bash
sudo ./deploy/openclaw-deploy.sh
```

`openclaw.json` is tracked intentionally as the current host-side configuration.
Review it before commits because it can contain local operational settings. The
backup and pre-update variants remain ignored.

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
├── public-service-gate/state.json
└── cron-state/
    ├── algeria-exhibitions-listener/state.json
    ├── github-issue-tracker/state.json
    └── webetu-meals/notification.log
```

These files are ignored by Git and excluded from deployment staging. They should
be treated like live data: back them up or inspect them directly when needed,
but do not upload them as source.

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
