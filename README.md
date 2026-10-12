# OpenClaw Gateway Source

This repository is the developer-owned layer around an OpenClaw install. It is
the single source of truth for the live gateway: custom plugins, command
wrappers, skills metadata, agent context, cron jobs, deployment helpers, and the
sanitized config template.

It does not own or patch the OpenClaw npm package itself. OpenClaw upgrades
replace only package-managed files; this repository tracks everything around them.

Production runs on **Railway** (project `openclaw-gateway`, service `gateway`,
environment `production`, EU West). Railway's GitHub integration watches `main`
and rebuilds on every push, so **merging to `main` is the deploy**. Git commits
and pushes are manual. See `deploy/railway/README.md` for the pipeline, the
one-time dashboard wiring, and the volume seed/recovery procedure.

Pull requests and `main` pushes run portable structure, documentation-contract,
syntax, unit, dependency, secret, and browser-image checks on GitHub-hosted
runners (`.github/workflows/deploy-main.yml`). There is no deploy job in that
workflow: the `validate` job is the gate, and Railway does the deploying.

## Runtime Layout

The gateway runs as a single Railway service. There is no second container: the
Job Scout browser worker and WhatsApp channel listener described below are built
in CI but **not currently deployed** (see *Job Scout sidecars*).

Durable state lives on a Railway volume mounted at `/home/joseph/.openclaw`:

- `workspace/` is the curated source overlay. The image build stages it from repo
  HEAD into `/opt/live-source` (`deploy/stage-openclaw-source.sh`, checked by
  `deploy/verify-staged-source.sh`), and the entrypoint rsyncs it onto the volume
  at boot. It changes only by deploying; nothing in the container edits it.
- `agent-workspace-admin/` is the `main` agent's private room. The entrypoint
  seeds it from `deploy/agent-workspace-admin/` plus five context files copied out
  of `workspace/` (`SOUL.md`, `HEARTBEAT.md`, `IDENTITY.md`, `MEMORY.md`,
  `USER.md`). `deploy/ci/check.py` asserts that list.
- `agent-workspace-public/` is the restricted public agent's room, seeded from
  `deploy/agent-workspace-public/`. It has no `MEMORY.md` and no `COMMANDS.md`,
  and the agent is limited to the sender-bound `public_service` tool. CI enforces
  the absence of those two files.
- `runtime/`, credentials, identity, state, and `openclaw.json` persist on the
  volume across deploys. `runtime/` is reseeded only by an explicit one-shot
  upload (see `deploy/railway/README.md`).

OpenClaw routes administrator DMs to the `main` agent and every other WhatsApp DM
to the restricted `public` agent. There is no approval step and no service menu:
every non-admin DM gets a conversational reply from the public agent, which
answers from `workspace/docs/business-info.md` (edit prices there) and hands a
customer off when they are ready to order or ask something the page does not
cover. Fruit-tree and building-material handoffs are forwarded to the partner on
WhatsApp; after any handoff the bot stays silent for that customer until an admin
sends `resume +<number>`. See `workspace/docs/public-services.md`.

## Structure

This is the repository tree. Paths are relative to the repo root.

```text
.
├── .gitignore                     # Default-deny tracking policy
├── railway.json                   # Railway build config -> deploy/railway/Dockerfile
├── config/
│   └── openclaw.template.json     # Sanitized config template, merged on deploy
├── deploy/
│   ├── railway/                   # The production image: Dockerfile, entrypoint, README
│   ├── stage-openclaw-source.sh   # Builds the curated workspace overlay
│   ├── verify-staged-source.sh    # Rejects a staged overlay with runtime/secret files
│   ├── stage-workspace.rsync      # The single deployable-workspace allowlist
│   ├── ci-check.sh                # Portable repository validation and tests
│   ├── ci/                        # Structure/docs policy and mappings
│   ├── patch-openclaw-config.py   # Applies the template to a live openclaw.json (NOT wired into the Railway boot)
│   ├── gateway-exec.sh            # How installers reach the live gateway
│   ├── install-*.sh               # Cron registration (promos, Job Scout workers)
│   ├── agent-workspace-admin/     # Private administrator context template
│   ├── agent-workspace-public/    # Restricted public context template
│   └── job-scout-*.Dockerfile     # Sidecar images, built in CI, not deployed
└── workspace/                     # Developer-owned source and agent context
    ├── bin/                       # Command wrappers (openclaw-cap, job-scout, ...)
    ├── lib/                       # Shared Python helpers
    ├── plugins/                   # OpenClaw plugins
    ├── docs/                       # Service policy and onboarding docs
    ├── cron/                      # Deterministic cron jobs (promo ads, listeners)
    ├── skills/                    # Custom skills + skills.lock.json
    ├── account-link/              # Account linking CLI
    ├── cv-maker/                  # Standalone CV maker service
    └── job-scout/                 # Job Scout service (maintained upstream)
```

`workspace/` holds source and context, not a working room: plugins, command
wrappers, scripts, docs, selected skills, and the five agent context files that
the entrypoint copies into the admin room.

On the live volume, `runtime/` holds mutable command state — account-link caches,
public-service routing state, cron state, and cron logs. It is volume data, not
source, and is neither tracked nor staged.

## Git Tracking

The repository uses a default-deny `.gitignore`: nothing is tracked unless it is
explicitly allowed.

Tracked categories:

- `config/openclaw.template.json`
- `deploy/**`
- the five agent context files (`workspace/SOUL.md`, `HEARTBEAT.md`,
  `IDENTITY.md`, `MEMORY.md`, `USER.md`)
- `workspace/bin/**`
- `workspace/lib/*.py`
- `workspace/plugins/**`
- `workspace/cron/**/*.py` and `workspace/cron/**/*.sh`
- `workspace/docs/**`
- custom source folders such as `account-link/`, `cv-maker/`,
  and `job-scout/` (including its `scrapers/`,
  `jobscout/`, and `channel-listener/` helper packages). Each service folder is
  staged whole by a per-service recursive glob in
  `deploy/stage-workspace.rsync` (e.g. `+ /job-scout/***`, `+ /cv-maker/***`), so
  adding or splitting an internal module ships automatically without editing the
  deploy allowlist; the global runtime/state/cache excludes at the top of that
  filter still keep `__pycache__/`, `node_modules/`, `*.log`, and `*.json` state
  files out of the deployable source. A **new** service folder is a different
  matter: the filter ends in `- /**`, and the repository `.gitignore` is
  default-deny, so a new tree has to be allowlisted in both or it will neither
  commit nor deploy. `workspace/cv-maker/` is listed in each for that reason. It
  is deliberately outside `workspace/job-scout/`, which is maintained upstream
  and synced in wholesale: an earlier CV maker implementation lived inside it and
  was silently deleted by a sync.
- selected custom skills plus `workspace/skills.lock.json`. Unlike the service
  folders above, skills are **not** covered by a recursive glob: each one is
  listed explicitly in `deploy/stage-workspace.rsync` (e.g. `+ /skills/cv-maker/`
  and `+ /skills/cv-maker/SKILL.md`) ahead of the closing `- /**` rule, so a new
  skill must be added there or it will not ship.

Ignored categories:

- `.env` and all secrets
- the live `openclaw.json`, generated from `config/openclaw.template.json`, and
  its backup/pre-update variants (`openclaw.json.last-good`,
  `openclaw.json.pre-update*`)
- credentials, devices, identity, sessions, memory DBs, logs, runtime DBs
- `npm/`, media, browser data, caches, Python bytecode, virtualenvs
- generated job/CV artifacts and runtime state files

Before pushing, inspect exactly what Git sees:

```bash
git status --short
git ls-files --others --exclude-standard
```

## Deploying Changes

Merging to `main` is the deploy:

```bash
git status --short
git commit -m "Describe the change"
git push origin main            # via a PR; see the gate below
```

GitHub Actions runs the `validate` job (portable structure and docs-contract
checks, shellcheck, actionlint, unit tests, `npm audit`), a gitleaks secret scan,
and — when the browser image inputs change — a Job Scout browser image build.
Railway's GitHub integration then rebuilds `deploy/railway/Dockerfile` from the
new `main` and redeploys the gateway.

**The CI gate depends on branch protection, which lives outside this repo.**
Railway deploys whatever lands on `main` regardless of the workflow result, so
the `Portable validation` check must be required on `main` for CI to actually
prevent a broken deploy. `deploy/railway/README.md` has the `gh api` command that
sets it. Verify it is in place before relying on it:

```bash
gh api repos/joe123-crypto/project_genaebot/branches/main/protection \
  --jq '.required_status_checks.contexts'
```

If that returns an error or an empty list, a failing commit pushed to `main` will
deploy. Roll production back by reverting the bad commit on `main`; the revert
goes through the same path.

Validate without deploying:

```bash
./deploy/ci-check.sh
```

`ci-check.sh` is host-independent and is exactly what CI runs. It also applies
`deploy/ci/docs-contract.json`: a change to a declared public, onboarding, or
operations interface must update the mapped documentation in the same pull
request or push.

To check that the deployable overlay still builds the way the image build will:

```bash
./deploy/stage-openclaw-source.sh . /tmp/stage
./deploy/verify-staged-source.sh /tmp/stage
```

### Job Scout sidecars (built, not deployed)

Two sidecar images are defined and CI-built but have **no Railway service**:

- `deploy/job-scout-browser.Dockerfile` — the Playwright worker that submits
  URL-only applications. `jobscout/config.py` defaults its endpoint to
  `http://job-scout-browser:8787/apply`, a hostname that does not resolve in
  production, so URL-only applications are recorded `action_required` rather than
  submitted.
- `deploy/job-scout-channel-listener.Dockerfile` — the Baileys listener that feeds
  the `zimlinks` WhatsApp Channel source.

Restoring either means adding a Railway service, not editing docs. Until then,
treat the channel-source and URL-apply features below as inactive.

### Job Scout scheduler activation

Scheduler activation is deliberately separate from a gateway deploy.

1. Set these as Railway service variables (never in Git):

```text
JOB_SCOUT_BROWSER_TOKEN=<at least 32 random bytes>
OPENROUTER_API_KEY=<OpenRouter API key, sk-or-...>
```

   `OPENROUTER_API_KEY` backs all LLM model calls (the agent brain and the Job
   Scout channel post classifier). The retained `AI_GATEWAY_API_KEY` (Vercel AI
   Gateway) is used only for memory-search embeddings, since OpenRouter has no
   embeddings endpoint.

2. Deploy the Agent Genaie API schema change, then merge this repository.

3. CVs live in the Agent Genaie Cloudflare R2 bucket (object key
   `<uid>/cv/cv.pdf`), and `cvFileRef` in Firestore holds that key. New CVs are
   uploaded to R2 by `stage-cv` / `save-profile`, and the worker downloads a
   presigned URL at dispatch time. The gateway converts uploaded PDFs into a
   structured A4 HTML template using Poppler and Tesseract OCR, with plain text
   extraction as a fallback. The conversion removes browser print headers, local
   `file://` paths, timestamps, and page counters before any personalized CV is
   rendered.

4. Any ready user without a CV in R2 must upload it again through the normal Job
   Scout profile flow. Local files under `runtime/job-scout/users` are disposable
   caches and are not evidence that an R2 CV exists or is missing.

5. Install or update the schedule:

```bash
./deploy/install-job-scout.sh
```

Cron jobs live in the gateway's own state on the Railway volume, so they survive
deploys and only need registering when they change. The installers reach the live
gateway through `deploy/gateway-exec.sh`, which uses `railway ssh` by default;
set `OPENCLAW_EXEC_MODE=docker` (or `DOCKER` / `OPENCLAW_CONTAINER`) to target a
local container instead.

The installer refuses to enable the cron unless at least one subscriber is ready
and every ready CV is accessible. The browser-worker token-parity preflight runs
only in docker mode, since there is no browser worker in production. It installs
an isolated `main` agent turn at `08:00 Africa/Harare` with a six-hour run budget.
The parent prepares every ready subscriber and delegates four isolated same-model
children at a time; children score the deterministic shortlist, while the
validated dispatcher remains the only component allowed to record, submit, or
send. For subscribers with `autoApply: false`, strong email and website matches
are recorded as `pending_approval` without creating personalized documents or
submitting an application. The dashboard exposes those records for approval. A
separate five-minute `job-scout-approvals` worker picks up records the user
changed to `approved`, then runs CV and application-letter customization before
the dispatcher sends email applications or submits supported website forms.

### Dashboard task telemetry

After each per-user run, the Job Scout dispatch publishes a live task snapshot
to Agent Genaie's `POST /internal/dashboard/tasks`
(rendered by the signed-in dashboard) through
`workspace/job-scout/jobscout/integrations/dashboard_telemetry.py`. Publishing is
best-effort: failures log one stderr warning and never change the run outcome. It
reuses `APP_BASE_URL` and `AGENT_GENAI_INTERNAL_API_KEY`, so no extra
configuration is needed.

### WhatsApp Channel sources (inactive)

Job Scout can consume job adverts from the ZimLinks, ZIM•JOB•VACANCIES, and
EveryDayVacancies WhatsApp Channels. This needs the separate
`job-scout-channel-listener` container, which has no Railway service, so the
source is currently inactive and contributes no candidates.

The listener uses its own linked-device auth and must never be pointed at
OpenClaw's WhatsApp auth directory. It captures posts in real time, backfills the
latest 10 from each channel after pairing/reconnect, and keeps transient state
under `runtime/job-scout-channel/`. The daily dispatch reads normalized Zimbabwe
candidates from that queue. Raw post text/media is removed once all selected
users reach terminal outcomes; application records remain in the Agent Genaie
database. Media-only posts are discarded: this version does not perform OCR.

Reactivating it means adding a Railway service for the image and restoring a
pairing path (the old `deploy/pair-job-scout-channel.sh` drove it through Docker
Compose and was removed with the rest of the Compose tooling).

### Group job feeds (Vacancy Lamp and Hustlers Marketplace)

The daily dispatch also writes a broad, deduplicated cache of Zimbabwe job
adverts (VacancyMail, iHarare, the WhatsApp Channels above, and the other broad
sources), and a separate delivery-only cron posts from that cache to a WhatsApp
group — the source is discovery; delivery just reads the cache. Two standalone
installers register these delivery crons (they are not part of the automatic
gateway deploy):

- `./deploy/install-job-scout-group-feed.sh` — posts the daily feed (up to
  5 jobs) to the main jobs group (Vacancy Lamp) at `09:20 Africa/Harare`. The
  cache is ordered so fully structured adverts (a clear title and location, an
  overview, both a Key Responsibilities and a Qualifications and Experience
  section, a non-expired closing date, and an actionable contact) are posted
  first, falling back to other eligible adverts only to fill the 5 daily slots.
  Set `JOB_SCOUT_GROUP_FEED_TARGET` to the group JID (`120…@g.us`), or
  `JOB_SCOUT_GROUP_FEED_GROUP_QUERY` once the bot is a member and the group
  appears in the OpenClaw WhatsApp directory.
- `./deploy/install-job-scout-hustlers-feed.sh` — posts **one job per day** from
  the same cache to the **Hustlers Marketplace** group at `09:40 Africa/Harare`,
  alongside the jobs-group invite link so members can join for more. Set
  `JOB_SCOUT_HUSTLERS_FEED_TARGET` to its group JID, or leave
  `JOB_SCOUT_HUSTLERS_FEED_GROUP_QUERY` (default `Hustlers Marketplace`) to
  resolve it from the directory. Override the appended invite with
  `JOB_SCOUT_HUSTLERS_FEED_INVITE_URL` (defaults to the main
  `JOB_SCOUT_GROUP_FEED_INVITE_URL`), and the daily cap with
  `JOB_SCOUT_HUSTLERS_FEED_MAX_MESSAGES` (default `1`).

Delivery is tracked per target, so a job posted to one group is still eligible
for the other; each group only ever receives a given advert once.

### Scheduled jobs

Cron jobs live in the gateway's own state on the Railway volume, so they survive
deploys. Each has an idempotent installer under `deploy/` that registers or
updates it; run one only when its schedule or command changes. All times are
`Africa/Harare`.

- **`cv-promo`** (`install-cv-promo.sh`, 11:00) runs
  `workspace/cron/cv-promo/post_cv_ad.py`, a deterministic (no-agent) job that
  posts the fixed CV-service ad to the WhatsApp group JIDs listed in
  `workspace/cron/cv-promo/targets.json`. Most groups get the ad daily; groups
  under the file's `weekly_groups` map are posted to only on their configured
  weekdays (the cron still runs daily and simply skips them on other days), which
  is how Vacancy Lamp advertises CV making twice a week (Mon and Thu)
- **`building-materials-promo`** (`install-building-materials-promo.sh`, 12:00)
  runs `workspace/cron/building-materials-promo/post_ad.py`, a deterministic
  (no-agent) job that posts the fixed building-materials for-sale ad to the WhatsApp group
  JIDs listed in `workspace/cron/building-materials-promo/targets.json`
- **`genaie-promo`** (`install-genaie-promo.sh`, 14:00) runs
  `workspace/cron/genaie-promo/post_ad.py`, a deterministic (no-agent) job that
  posts the fixed Genaie-services ad (group advertising, poster design, WhatsApp
  bots) to the WhatsApp group JIDs listed in
  `workspace/cron/genaie-promo/targets.json` (every group the bot is in except
  Vacancy Lamp)
- **`genaie-leads-promo`** (`install-genaie-leads-promo.sh`, 17:00) runs
  `workspace/cron/genaie-leads-promo/post_ad.py`, a deterministic (no-agent) job
  that posts the fixed Genaie lead-generation ad ("we generate potential customers;
  you choose how you pay" — pay per lead, fixed monthly, or a % of revenue) to the
  WhatsApp group JIDs listed in `workspace/cron/genaie-leads-promo/targets.json`.
  This is a separate campaign from `genaie-promo` above (later slot, own image and
  copy), reusing the same shared `promo_broadcast` delivery engine
- **`fruit-trees-promo`** (`install-fruit-trees-promo.sh`, 09:00) runs
  `workspace/cron/fruit-trees-promo/post_ad.py`, a deterministic (no-agent) job that
  posts the fixed grafted-fruit-tree for-sale ad (mango, pawpaw, orange, naartjie,
  lemon; retail/wholesale pricing) to the WhatsApp group JIDs listed in
  `workspace/cron/fruit-trees-promo/targets.json`, reusing the same shared
  `promo_broadcast` delivery engine
- **`borehole-drilling-promo`** (`install-borehole-drilling-promo.sh`, 08:00) runs
  `workspace/cron/borehole-drilling-promo/post_ad.py`, a deterministic (no-agent) job
  that posts the fixed solar-borehole installation ad (0.75HP/1HP/1.5HP/2HP DC pump
  packages, each with tank, stand, solar and installation) to the WhatsApp group JIDs
  listed in `workspace/cron/borehole-drilling-promo/targets.json`, rotating through four
  bundled photos daily and reusing the same shared `promo_broadcast` delivery engine.
  Its fourth caption variant ("El Nino is coming. Are you prepared?") cites and links
  the Meteorological Services Department 2026-2027 seasonal forecast and always posts
  with `assets/elnino.jpg` instead of the rotating photo.
  Most groups get the ad daily; groups under the file's `weekly_groups` map are posted
  to only on their configured weekdays, which is how MUTANDIRI VILLAGE RESIDENTS gets
  the ad three times a week (Mon, Wed and Fri)

All six promo crons rotate between caption variants (the original operator copy
plus rewordings with identical facts; three each, four for the borehole promo) and
carry a per-group tracking link. See *Promo caption rotation and ad ref tracking*
below.

The daily Job Scout turn (`install-job-scout.sh`, 08:00), the five-minute
`job-scout-approvals` worker, the hourly `job-scout-cv-conversion` pass and the
two group feeds have their own installers and are described above.

### Promo caption rotation and ad ref tracking

The shared promo engine (`workspace/lib/promo_broadcast.py`) accepts either one
caption string (sent verbatim, as before) or a list of caption variants. Each
promo now supplies three (the borehole promo four): its original operator copy as
variant 1, plus rewordings that keep every fact identical (the borehole El Nino
variant may additionally carry its cited forecast link, listed in `SOURCE_URLS`).
A group's variant is `(post counter + sha256(group JID)) % <variant count>`, so a
group never gets the same caption
on two consecutive posts (weekly-limited groups count their own posting days) and
groups are spread across variants on a given day. A promo may pass
`variant_images={variant_index: image_path}` to pin a fixed image to one variant
(the borehole El Nino variant uses `assets/elnino.jpg`); other variants keep the
daily image rotation. `--dry-run` shows each group's variant, ref and any pinned
image.

Each promo also passes a `TRACKING` config (opt-in; a promo without one sends
exactly its caption). Every group gets a stable ref code: a promo prefix plus
three characters derived from the group JID, e.g. `BH-VD4`. The caption is sent
with an extra line `Tap to chat on WhatsApp: https://wa.me/<number>?text=...`
whose prefilled text reads `Hi, I saw your borehole ad (ref BH-VD4)`. A caption
may instead place the link itself with the `{ref_link}` / `{ref}` placeholders.

| Promo | Prefix | Link target |
|---|---|---|
| borehole-drilling-promo | BH | +263785632943 (bot) |
| building-materials-promo | BM | +263785632943 (bot) |
| cv-promo | CV | +263785632943 (bot) |
| fruit-trees-promo | FT | +263785632943 (bot) |
| genaie-leads-promo | GL | +263785632943 (bot) |
| genaie-promo | GN | +263785632943 (bot) |

Decode refs with `--refs`, which prints `ref  promo  group_jid  link` for every
target and exits without sending or touching state:

```bash
python3 workspace/cron/borehole-drilling-promo/post_ad.py --refs
```

When a DM carrying a ref reaches the bot, the public-service-gate plugin counts it
in `runtime/public-service-gate/ad-refs.json` (`count`, `uniqueSenders`,
`firstSeen`, `lastSeen` per ref; senders stored only as a short one-way hash).
Recording is observe-only and never changes the reply; admin DMs and group
messages are not counted. Every promo links to the bot's own number, so every
reply is counted and answered by the bot, which forwards partner orders (fruit
trees, building materials) to the partner when it hands the customer off. Keep the prefix list in
`workspace/plugins/public-service-gate/lib/ad-refs.js` in sync with the promos'
`TRACKING` prefixes (a test enforces this).

## Config Changes

The live `openclaw.json` lives on the Railway volume, so editing a local copy
changes nothing. For stable deterministic config changes, edit:

```text
config/openclaw.template.json
```

**but be aware the template is not applied automatically.** `deploy/railway/entrypoint.sh`
never runs `deploy/patch-openclaw-config.py`; that merge was a step in the retired
self-hosted deploy script. The applier is still here and still tested, but nothing
on the Railway boot path calls it, so editing the template alone changes nothing
in production.

The one exception is the public WhatsApp system prompt at
`workspace/docs/public-whatsapp-system-prompt.txt`. On every boot, after the
OpenClaw upgrade step and before the gateway starts, the entrypoint runs
`deploy/railway/sync-public-prompt.py`, which writes that file into
`channels.whatsapp.direct["*"].systemPrompt` and touches nothing else in the
config (peer overrides included). It logs `public WhatsApp prompt updated` or
`unchanged`; any problem (missing or empty file, bad JSON) is logged as
`NOT synced` and the boot carries on with the previous prompt. So a prompt edit
reaches the live agent on the next deploy, and the business facts it reads
(`workspace/docs/business-info.md`) are synced with the rest of `workspace/`.

Any other config change has to be applied to the volume deliberately until the
full applier is wired up (see the warning below), for example by running the
applier inside the container:

```bash
railway ssh -- python3 /home/joseph/.openclaw/deploy/patch-openclaw-config.py --help
```

> **Removing a key from a plugin's `configSchema` can take production down.**
> `workspace/plugins/public-service-gate/openclaw.plugin.json` sets
> `additionalProperties: false`, and because the template is not applied, the live
> `openclaw.json` still carries keys the template has dropped. Deleting such a key
> from the schema makes the live config invalid and the gateway refuses to start,
> which is exactly what crashlooped production on 2026-10-12. Four keys are kept
> declared-but-unread for this reason, each with a `description` saying so, and
> `deploy/test_plugin_config_schema.py` pins the live key set so CI fails before a
> deploy does. Remove a key from the live config first, then from the schema.

The template remains the tracked source of truth for what the config *should* be;
`openclaw.json` and its backup/pre-update variants are gitignored.

## Operational Changes

Runtime and user operations go through approved commands rather than hand-edited
config. Shell into the live gateway with `railway ssh`:

```bash
railway ssh -- /home/joseph/.openclaw/workspace/bin/openclaw-cap --help
```

Examples:

```bash
railway ssh -- /home/joseph/.openclaw/workspace/bin/openclaw-cap users list

railway ssh -- /home/joseph/.openclaw/workspace/bin/openclaw-cap \
  account-link status --phone +213...

railway ssh -- /home/joseph/.openclaw/workspace/bin/openclaw-cap \
  cron run algeria-exhibitions --dry-run
```

`railway link` once in this checkout so `railway ssh` resolves the project,
service and environment (see `deploy/railway/README.md`).

Rule of thumb:

- Source, plugin, script and template changes require a deploy (merge to `main`).
- Runtime user operations use `openclaw-cap`.
- Runtime state under `runtime/` lives on the volume, survives deploys, and is
  never committed.
- Git commits and pushes are always manual.

## Runtime Volume Data

Mutable service data lives on the Railway volume under `/home/joseph/.openclaw`:

```text
runtime/
├── account-link/pending-links.json
├── public-service-gate/invites.json (member store; invite codes retired)
├── public-service-gate/state.json
├── public-service-gate/ad-refs.json (promo ad ref code counts)
├── public-service-gate/handoffs.json (customers handed off to a person; bot silent)
├── job-scout/ (dispatch runs, per-user CV caches)
└── cron-state/
    ├── algeria-exhibitions-listener/state.json
    └── github-issue-tracker/state.json
```

These files are ignored by Git and excluded from deployment staging. They are live
data on the volume, not source: inspect or back them up through `railway ssh`, and
never commit them. A deploy never overwrites them; `runtime/` is reseeded only by
an explicit one-shot upload (see `deploy/railway/README.md`).

The deployable source list is controlled by `deploy/stage-workspace.rsync`. To see
exactly what a deploy would stage:

```bash
./deploy/stage-openclaw-source.sh . /tmp/stage
find /tmp/stage -type f | sort
```

To see what Git can upload:

```bash
git status --short
git ls-files
```

## OpenClaw Updates

The gateway image pins OpenClaw via `npm install -g openclaw@<version>` in
`deploy/railway/Dockerfile`. The current pin is `openclaw@2026.9.9` (the `latest`
stable channel), which requires Node `>=24.16`, so the image uses
`node:24-bookworm-slim`.

A new OpenClaw release refuses to start on state written by an older one until
`openclaw doctor --fix` has run. The entrypoint therefore runs a one-time upgrade
migration before the gateway until `.openclaw-upgrade-<version>.done` exists on
the volume (or whenever `meta.lastTouchedVersion` in `openclaw.json` differs from
the installed version, or an npm-installed plugin in `openclaw plugins list
--json` is not at the installed version). It snapshots the config to
`openclaw.json.pre-upgrade-<version>`, runs doctor until it converges (upgrading
the external `@openclaw/*` plugins on the volume to the matching release), runs
`openclaw plugins update` for any plugin still behind, restores the WhatsApp
`allowFrom`/`groupAllowFrom` lists from the snapshot (doctor would otherwise add
`"*"`), and validates. One-off commands (`railway ssh -- …`) skip this step.

Doctor only warns when it cannot update a plugin, and a plugin built for an older
core fails to register (for WhatsApp: the bot stops replying). npm runs as
`joseph`, so `/home/joseph/.npm` must be writable by it: the image deletes the
cache `npm install -g` leaves there as root, and the entrypoint chowns it at boot.
If a plugin still cannot be updated, the entrypoint logs
`WARNING: plugins still not at <version>` and retries on the next boot.

The 2026.9 config schema moved `agents.list` to keyed `agents.entries`,
`agents.defaults.memorySearch` to `memory.search`, `media` to `attachments`, and
WhatsApp `blockStreaming*` to `channels.whatsapp.streaming.block`; it removed
`web.whatsapp` (its socket timings are now the plugin defaults) and requires an
explicit `plugins.entries.voice-call.config.agentId` when several agents exist.
The template uses the new shape. To roll back, redeploy the previous image and
restore `openclaw.json.pre-upgrade-<version>` plus the
`*.pre-startup-migration-*.bak` SQLite backups doctor leaves next to each
database.

This repo must not edit package-managed files under the installed OpenClaw
package. After an OpenClaw bump, run `./deploy/ci-check.sh` and confirm the image
still builds:

```bash
docker build -f deploy/railway/Dockerfile .
```
