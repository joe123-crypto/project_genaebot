# OpenClaw Gateway Source

This repository is the developer-owned layer around an OpenClaw install. It is
the single source of truth for the live gateway: custom plugins, command
wrappers, skills metadata, agent context, cron jobs, deployment helpers, and the
sanitized config template.

OpenClaw upgrades replace package-managed files. The image applies one guarded,
version-specific catalog-worker patch; it is disabled by default and requires
a new compatibility review whenever the pinned package changes.

Production runs on **Railway** (project `openclaw-gateway`, service `gateway`,
environment `production`, EU West). `.github/workflows/deploy-main.yml` validates
PRs and `main`, builds the pinned production runtime for integration tests, and
releases `main` only after all three checks pass. The release job uploads an
archive of the exact tested commit, verifies startup provenance, the connected
main WhatsApp account and the enabled jobs, and restores the previous image on
failure. The advertising account is optional for release readiness: an offline
ad line is reported as degraded without blocking a healthy main-line deployment.

Before releasing, the job installs pinned Railway CLI 5.71.0 with up to three
attempts for transient registry or GitHub binary-download failures. If installation
still fails, the release step never runs; re-run the failed job in GitHub Actions.

This gate requires the production-scoped `RAILWAY_TOKEN` GitHub Actions secret
and Railway's direct GitHub auto-deploy to be disconnected. Until the one-time
cutover is completed, the existing direct integration still deploys `main`
without waiting for these checks. See [deployment setup](deploy/railway/README.md#one-time-wiring)
for the credential, cutover and recovery procedure.

## Runtime Layout

The gateway runs as a single Railway service. There is no second container.

The bot does two things: the daily promo ad crons (cv-promo, genaie-promo,
fruit-trees-promo and the rest, see *Scheduled jobs*) and paid CV making for
public WhatsApp users. The daily job feeds and their scraping pipeline are retired;
Vacancy Lamp and Hustlers Marketplace retain their configured promotions.

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
│   ├── gateway-exec.sh            # How a deploy script reaches the live gateway
│   ├── gateway-campaigns.sh       # The live campaign list, for the release gates
│   ├── backup-operator-data.py    # The dated copy of the operator's Firestore/R2 data
│   ├── agent-workspace-admin/     # Private administrator context template
│   ├── retire-job-scout-crons.sh  # One-off removal of the retired Job Scout crons
│   └── agent-workspace-public/    # Restricted public context template
└── workspace/                     # Developer-owned source and agent context
    ├── bin/                       # Command wrappers (openclaw-cap, cv-maker, ...)
    ├── lib/                       # Shared Python helpers
    ├── plugins/                   # OpenClaw plugins
    ├── docs/                       # Service policy and onboarding docs
    ├── cron/                      # Deterministic cron jobs (promo ads)
    ├── skills/                    # Custom skills + skills.lock.json
    └── cv-maker/                  # Standalone CV maker service
```

`workspace/` holds source and context, not a working room: plugins, command
wrappers, scripts, docs, selected skills, and the five agent context files that
the entrypoint copies into the admin room.

On the live volume, `runtime/` holds mutable command state — CV orders,
public-service routing state, cron state, and cron logs. It is volume data,
not source, and is neither tracked nor staged.

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
- `workspace/docs/**`
- the service folder `cv-maker/` (including its internal packages). It is
  staged whole by a per-service recursive glob in
  `deploy/stage-workspace.rsync` (`+ /cv-maker/***`), so
  adding or splitting an internal module ships automatically without editing the
  deploy allowlist; the global runtime/state/cache excludes at the top of that
  filter still keep `__pycache__/`, `node_modules/`, `*.log`, and `*.json` state
  files out of the deployable source. A **new** service folder is a different
  matter: the filter ends in `- /**`, and the repository `.gitignore` is
  default-deny, so a new tree has to be allowlisted in both or it will neither
  commit nor deploy. `workspace/cv-maker/` is listed in each for that reason.
- selected custom skills plus `workspace/skills.lock.json`. Unlike the service
  folder above, skills are **not** covered by a recursive glob: each one is
  listed explicitly in `deploy/stage-workspace.rsync` (e.g. `+ /skills/cv-maker/`
  and `+ /skills/cv-maker/SKILL.md`) ahead of the closing `- /**` rule, so a new
  skill must be added there or it will not ship.

**Not tracked, and deliberately not trackable: the operator's own data.** The
groups the bot advertises in, their names and numbers, every campaign's copy,
prices and pictures, and each campaign's schedule and switch used to be files
here -- `workspace/cron/membership.json`, ten `targets.json` generated from it,
ten `post_ad.py`, their `assets/` directories, `deploy/promo-crons.json` and a
mirror of the picture index. They live in Firestore and Cloudflare R2 now and
are edited in the console. What replaced git as the copy of record is
`deploy/backup-operator-data.py` (see "Backups of the operator data"); git
history still holds every one of those files up to the commit that deleted them.

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

Open a PR from a dedicated branch, inspect its diff, and merge after these
checks pass: `Portable validation`, `Secret and private-state scan`, and
`Installed runtime compatibility`. The compatibility job uses Node 26.12.1 and
the installed OpenClaw 2026.9.9 SDK, SQLite and real catalog workers; missing
integration dependencies fail that job instead of silently skipping tests.

Before merging, perform the [live cron preflight](deploy/railway/README.md#cron-preflight-before-merging).
Require zero active runs and choose a gap that allows CI, deployment, startup
verification and recovery to finish before the next campaign. The main workflow
spends about ten minutes testing the runtime before it can begin deployment.
The deploy job also waits (up to 45 minutes) for a quiet window computed from
the live campaign list it reads off the gateway, and fails with the next quiet
time rather than restart the gateway mid-run; see the preflight section for the
override.

After the [one-time cutover](deploy/railway/README.md#one-time-wiring), the
serialized `Deploy verified production commit` job is the production gate even
when branch protection is unavailable. Superseded commits are skipped. The job
archives the tested SHA, records the new deployment ID and image digest, and
checks the baked source marker, selected runtime flags, both connected accounts
and all ten campaigns. A failed release restores the exact
previous image and owned runtime settings, then checks its startup. Verification
allows time for the entrypoint's 330-second stale-lease handoff before the gateway
can begin connecting its accounts.

Release and compatibility results remain attached to the workflow as artifacts.
A normal push preserves the current runtime settings, including sender transport
and the optional expired WhatsApp metadata cleanup. Manual workflow runs default to `preserve`; explicitly choose `baseline`
to disable retirement or `catalog-trial` to enable it. A missing retirement setting
uses the image baseline (disabled); unrecognized values stop the release before
any production changes. Release artifacts record both the requested and resolved
profile. See [cost measurement](deploy/railway/README.md#cost-measurement).

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

### Retired job feeds

The Vacancy Lamp and Hustlers Marketplace daily job feeds are retired, including
all four board scrapers, cache refresh and job delivery commands. Both WhatsApp
groups remain in membership and keep their configured promotional campaigns.

Cron registrations persist independently of source deployments. Before deploying
this removal, record the feed definitions for recovery, wait until neither feed
is running, then use `./deploy/retire-job-scout-crons.sh --dry-run` and
`./deploy/retire-job-scout-crons.sh`. The helper removes current and legacy feed
names and is safe to repeat. It also removes the previously retired Job Scout jobs.
After deployment, verify no feed registration or executable remains. Move only
`runtime/job-scout/group-feed/` to a dated directory under `retired/job-feeds/`
outside active runtime; retain cron receipts and unrelated runtime state.

#### Moving off Job Scout (one-off, after the deploy that removed it)

Job Scout (per-user job hunting and applications), account-link, the Gmail and
Calendar tools, and the algeria-exhibitions and github-issue-tracker crons were
removed. Their crons may still be registered in the gateway and would fail daily,
so after deploying run:

```bash
./deploy/retire-job-scout-crons.sh --dry-run   # list what would be removed
./deploy/retire-job-scout-crons.sh             # remove job-scout, job-scout-approvals, ...
```

The public-service-gate manifest still declares `jobScoutOnboardingEnabled` and
`pendingLinksPath` (like `newsEnabled` and `eventScoutEnabled`) because the live
`openclaw.json` still carries them and the schema rejects unknown keys. Remove
them from the live config first, then from the manifest.

### Scheduled jobs

Cron jobs live in the gateway's own state on the Railway volume, so they survive
deploys. All times are `Africa/Harare`.

**Every campaign is one Firestore document and every job runs one shared
runner.** There is no per-campaign script, installer or manifest in this
repository any more. `campaigns/<slug>` carries the campaign's copy, its
pictures, its schedule, its run timeout, its failure-alert number and its
on/off switch; the groups it posts to are the `promos` lists on the
`groups/<jid>` documents; and the job's command is always
`workspace/bin/run-console-campaign <slug>`. All of that is edited in the
console (see *Operator Console API*), which writes Firestore and lets the
campaign reconciler bring the gateway's cron into line.

Ask the gateway what the campaigns actually are rather than reading a list here:

```bash
./deploy/gateway-campaigns.sh | python3 -m json.tool   # every campaign, as the console sees it
railway ssh -- openclaw cron list --all --json         # the jobs themselves
```

A campaign converted from a repository script **keeps the job name it always
had** -- `<slug>-promo` rather than `console-<slug>-promo` -- recorded as
`jobName` on its document. That is deliberate: the job name is also the
`runtime/cron-state/<jobName>/` directory holding which groups were sent to
today and where the caption rotation stands, so keeping the name carried that
state across the conversion instead of re-posting to every group.

Two switches, and a campaign posts only if both are on: `enabled` on the
document is the operator's live pause control, and `scheduleStatus == "ready"`
is the reconciler's confirmation that the gateway really has the cron. The
console reports them separately (`storeEnabled`, `cronEnabled`) so a screen
never offers a toggle for a campaign that cannot post either way.

A campaign's captions rotate between variants carrying a per-group tracking
link; see *Promo caption rotation and ad ref tracking* below. The group load is
split across the bot's two WhatsApp lines per group, so a campaign can post from
either or both.

### Two things a campaign's schedule has to be able to deliver

Both used to be repository tests, because every input was a committed file: the
group counts from ten `targets.json`, the times and timeouts from
`deploy/promo-crons.json`. They are operator data now, so they are checked
against the live list instead -- by `deploy/gateway_campaigns.py`'s `problems()`,
which `deploy/promo-deploy-window.py` runs on **every release**:

- **A campaign must finish inside its own job timeout.** The engine's worst case
  is the full start jitter plus every gap at the top of its range plus a
  per-send allowance, which at 20-60 s gaps is roughly `900 + 72n` seconds for
  `n` groups. Past the timeout the gateway kills the run with groups unreached,
  every day, and no deploy changes that -- so this **fails the release gate**.
  The fix is to raise that campaign's run timeout in the console or to split it.
- **Two campaigns must not overlap on one WhatsApp line.** The 20-60 s spacing
  is per account: two runs posting to the same line at once double its send rate
  and defeat the spacing, which is what happened on 2026-10-20 when fruit-trees
  (44 groups, 08:40) was still sending as skylight-solar started and the two
  interleaved two seconds apart. A campaign on the second line therefore never
  has to clear the first line's slots. Spacing degrades rather than breaks, so
  this is a **warning**, not a refusal.

A third warning comes from the same place: a campaign whose `tracking.prefix`
is not in `AD_REF_PREFIXES` in
`workspace/plugins/public-service-gate/lib/ad-refs.js` still posts, but the
reply recorder cannot count a single lead it brings in. That list is code and
the prefixes are data, so the gate is the only place the two can be compared.

Separately, `deploy/promo-deploy-window.py` holds a release until no campaign
may be sending: a gateway restart stops an in-flight run after the 315 s drain
and there is no catch-up, so a release inside a campaign's window costs that
campaign its whole day (which is what #184 did to fruit-trees on 2026-11-09).
It reads the same live list, so a campaign created in the console is visible to
it -- which it was not while the gate read committed files. If the gateway
cannot be reached the gate refuses rather than guesses, and the workflow's
`ignore_promo_window` input is the deliberate override.

How it reads the list depends on who is asking. CI fetches it over the console
API with `CONSOLE_API_GATE_TOKEN`, a bearer token the API accepts on that one
route and refuses on every write -- a gate that only reads should not carry the
authority to change anything. `railway ssh`, which `./deploy/gateway-campaigns.sh`
uses, is the operator path: it needs an SSH key registered with the Railway
account, which a GitHub runner has no way to have, and that is why the first
release after this migration failed its own gate with the gateway healthy. See
*The release gates' read-scoped token* in `deploy/railway/README.md`.

### Send spacing and the start jitter

The shared engine spaces consecutive group sends by a **random 20-60 s gap**
(uniform), not a fixed delay, because WhatsApp spam detection reportedly flags
machine-regular send intervals (before this change the logs showed one send every
~10 s, like a metronome). The gap is only taken between two send attempts: not
before the first or after the last, not for groups already sent today or off
their weekly day, and never on `--dry-run` / `--refs`. Override the bounds with
`PROMO_SEND_DELAY_MIN` / `PROMO_SEND_DELAY_MAX` (seconds) in the gateway env.

The run is planned to fit the job timeout it is given, which is the
`timeoutSeconds` on the campaign's own document -- the run has already read it,
so nothing has to ask the gateway for it. If the run would not fit, the engine
tightens the gaps proportionally (keeping them random, never below 5 s) and logs
a warning naming the remedy. A timeout too short to fit the group count even at
the 5 s floor (under ~370 s for a 20-group campaign) is the one case that can
still be cut short: the warning then says so and names the minimum the job
needs. Because each delivered group is committed to the campaign's state file
immediately, a run killed part-way and re-run the same day skips the groups it
already reached.

Each campaign also waits a **random 0-900 s before its first send**
(`PROMO_START_JITTER_SECONDS`; `0` disables it). The gaps inside a run were
already random, but every cron fires at one fixed minute, so without it the
account's day-to-day pattern stays perfectly periodic. Schedules are also kept
off the hour (minute != 0). The wait is charged against the timeout budget, so
the gap planning above sees only what is left, and it is capped at a quarter of
the job timeout so a short timeout clamps the wait instead of burning the run
before its first send. It is never taken on `--dry-run` / `--refs` or when no
group is pending.

The job feeds are retired; see *Retired job feeds* above.

### Group membership

Every group the bot knows about is one `groups/<jid>` document in Firestore:
its name, `source`, `status`, activity tier, posting account, daily-cap override
and the campaigns that target it. There is no committed roster and no generated
per-campaign target file; a run derives its target list from these documents in
one read (`promo_config.resolve`), and the console is where they are edited.

`status` does the work the prose exclusion lists used to: only `active` groups
are posted to, so an `admin-only` (sends return `forbidden`), `not-a-member`
(`item-not-found`) or `on-hold` group is excluded by its record while
**keeping** its `promos` list -- which is what makes resuming one a status flip
rather than a rediscovery. A single assignment can be paused on its own
(`paused_promos`), which the console shows so it can be resumed.

**Never hand-edit the gateway's session database to add names** (doing so took
the bot down on 2026-10-14). The supported gateway discovery API below handles
its own conversation records.

`workspace/bin/list-groups` prints the configured advertising roster
(`--caps` adds today's allowance, `--json` one object per group); it never
contacts WhatsApp and cannot discover newly joined groups.

### Automatic group discovery

The WhatsApp gateway saves group metadata automatically after each account
connects and hourly while connected. It uses the existing account socket's
`groupFetchAllParticipating()` call, so it opens no competing login and has no
100-result limit. Discovery is deterministic: it runs no agent and sends no
messages or notifications. Reconnection triggers a new scan; shutdown and
overnight standby stop the timers.

Each account keeps a private, atomically replaced inventory at
`$OPENCLAW_STATE_DIR/runtime/whatsapp-groups/<accountId>.json` (the state root
defaults to `/home/joseph/.openclaw`). This is outside the boot-synced workspace,
so observations survive redeploys. Only IDs, names, first/last-seen times,
member totals with observation times, and scan health are saved, never member
lists, message contents or credentials. Names
refresh when WhatsApp returns a nonblank subject; missing names retain the last
known name. Lookup failures retain previous observations. A 45-second timeout
reports failure without piling requests onto a still-pending lookup; replacing
the connection permits a fresh scan. Old connections cannot overwrite newer
observations. A damaged or unwritable inventory reports a warning without
interrupting the account.

The Groups API adds `discoveredGroups` and `groupDiscovery`. The console shows
an **Unassigned** section with each new group's full JID, name, observed accounts
and last-seen time. A JID observed by both lines appears once with both accounts.
Groups already present in the effective advertising configuration are excluded,
including on-hold, admin-only, not-a-member and deliberately cleared records.
Configured groups, campaign reach and overview totals keep their existing
meaning. Observations older than two hours are shown as stale; saved history is
not proof of current membership. Failed scans and disabled discovery remain
visible beside the retained observations.

**Discovery never assigns campaigns and never writes the operator config.** It
needs no write key. Choose campaigns separately through the operator workflow. `GENAIE_GROUP_DISCOVERY=0` disables scanning while retaining saved data.
The console can also run against an older bot that omits the new fields.

The scanner is integrated through the existing version-checked WhatsApp loader.
The compatibility suite checks its hooks against the actual pinned adapter,
executes the upstream lifecycle fixture, and tests discovery and persistence
offline. A changed discovery hook disables only discovery with a warning; it
does not prevent a healthy account from connecting.

### Discovering WhatsApp group IDs and names live

For a manual probe, verified on 2026-11-08 with OpenClaw 2026.9.9, use **`conversations.list` through
the running gateway**. With `channel: "whatsapp"`, this handler calls the
WhatsApp plugin's live directory lookup through the gateway's existing account
connections. WhatsApp's group metadata supplies both the JID and the subject
(group name), even when no sender-key file exists for the new group yet.

From this Railway-linked checkout:

```sh
source ./deploy/gateway-exec.sh

# Account health only; this does not list groups.
"${GATEWAY_OPENCLAW[@]}" channels status --probe --json

# Live discovery plus previously registered conversations.
"${GATEWAY_OPENCLAW[@]}" gateway call conversations.list \
  --params '{"agentId":"main","channel":"whatsapp","limit":100}' \
  --json --timeout 45000
```

The helper supplies gateway authentication and removes the conflicting
`OPENCLAW_HOME` override. Use `agentId: "main"` because the configured group
routes belong to that agent. The result's `conversations` array includes:

- `accountId`: the joined line, such as `ads` or `default`.
- `kind`: select `group`, excluding direct conversations.
- `target`: the WhatsApp group JID to record as a `groups/<jid>` document.
- `label`: the group name returned from WhatsApp metadata when available.

Filter the result to the intended account and compare its group JIDs with the
configured roster (`workspace/bin/list-groups --json`). There is no account
selector in this RPC request;
discovery runs across the configured accounts. `limit` must be at most **100**.
If needed, add a `query` string to narrow discovery and results, for example:

```sh
"${GATEWAY_OPENCLAW[@]}" gateway call conversations.list \
  --params '{"agentId":"main","channel":"whatsapp","query":"Advertisement Hub","limit":100}' \
  --json --timeout 45000
```

The response combines live discovery with persisted conversation records. Old
rows alone do not prove current membership, and a live-directory failure can
fall back to configured entries (check gateway warnings). A missing result,
especially at the 100-row limit, does not prove the account has not joined.
This lookup may register discovered addresses in the gateway's conversation
registry; it does not send messages or change campaign targeting.

The successful discovery on 2026-11-08 returned `accountId: "ads"`,
`target: "120363407345824911@g.us"` and
`label: "The Advertisement Hub (Buy & Sell)"`. That JID was missing from the
local advertising roster and the ads line's sender-key files.

Earlier approaches missed this:

- Sender-key filenames expose IDs but no group names, and a newly joined group
  may not have a sender-key file yet. They are only a fallback clue.
- `openclaw directory groups list --channel whatsapp --account ads --json`
  runs in a separate process and can fail with **"WhatsApp live groups are
  unavailable because the account is owned by another process."** This is an
  ownership limitation, not evidence that the group is absent. Use the gateway
  RPC above; do not bypass the ownership guard or restart the account to list it.

After identifying a group, get the intended campaigns from the operator and add
the group in the console. Discovery itself does not authorize adding campaigns,
committing, pushing, merging or deploying.

### Where the operator config lives

Every group, campaign, line and picture index is a Firestore document, and
`workspace/lib/operator_config.py` is what reads them: `settings/defaults`,
`lines/<id>`, `campaigns/<slug>` and `groups/<jid>`, assembled into one
in-memory config. Each document also carries `rank`, because Firestore lists by
ID while send order decides which groups a short run reaches.

This replaced a committed `membership.json` with ten generated `targets.json`
files beside it. Those could not be edited at runtime at all -- `entrypoint.sh`
rsyncs `workspace/` from the image with `--delete` on every boot, so changing a
caption or pausing a group cost a commit, a PR, CI, a build and a gateway
restart. `GENAIE_CONFIG_SOURCE` used to choose between the file, the store, and
a shadow mode that read both and compared them; the files are gone and the
variable selects nothing. Setting it anyway is harmless and says so in the
resolver's notes rather than failing the read.

`promo_config.resolve` answers the question **once per run** and hands back the
targets payload, the config the caps derive from, and each campaign's on/off
switch. One read matters: the engine used to open the targets file five times,
so a config change landing mid-run could split one run across two configs.

- **Targeting fails closed.** If Firestore is unreachable, the last-known-good
  snapshot in `runtime/firestore/` is used -- but only while it is younger than
  `operator_config.SNAPSHOT_MAX_AGE_SECONDS` (48 h). With no snapshot, or one
  past that age, `load()` raises, the run sends nothing and exits non-zero so
  the cron's failure alert fires. "Fail closed" has to mean closed rather than
  stale: a snapshot served indefinitely would keep advertising to a group an
  operator had paused days ago. **There is deliberately no second source.** The
  file that used to sit here was kept as a rollback and, by the time it was
  deleted, had drifted 58 fields from the store -- so falling back to it would
  have re-posted into groups someone had paused and advertised to groups that
  had been removed.
- **Validation is per document.** A malformed group is quarantined (excluded
  and reported) without touching the rest. A malformed settings, line or
  campaign document falls back to the snapshot. Deleting a campaign drops it
  from groups' lists with a warning, rather than quarantining every group that
  listed it.
- Transport is Firestore REST over `urllib`, because the image has no package
  install step. `workspace/lib/gcp_token.mjs` signs the service-account JWT
  with Node's built-in `node:crypto`, the one thing the standard library cannot
  do. The key reaches it over stdin, never argv.
- Each read diffs against the previous snapshot and appends what changed to
  `runtime/firestore/changes-YYYY-MM.jsonl`, since console edits keep no
  history of their own. The `audit` collection is the fuller record; see
  *Backups of the operator data*.

A campaign whose `campaigns/<slug>.enabled` is false **sends nothing even though
its cron is still registered** on the volume: pausing a campaign is a config
edit, not a deploy. A `--dry-run` of a paused campaign still previews, matching
`promo_caps`, which never treats the campaign being run as disabled.

The gateway's key is the read-only runtime key (Cloud Datastore Viewer),
base64-encoded in `GCP_SERVICE_ACCOUNT_B64` because the entrypoint's variable
sanitiser would mangle a multi-line JSON value; it is in the entrypoint's
`SECRET_KEYS`, so it lands in the volume `.env` at 0600. Writes need Cloud
Datastore User and go through the console API's own
`GCP_SERVICE_ACCOUNT_WRITE_B64`, or, from the operator's machine, the seed key.
Neither variable is release-managed, so a deploy neither sets nor clears them.

`console-read overview` carries `configSource` -- whether the read was live or
came from the snapshot, and any quarantined groups -- and `list-groups` names
the store it read in its header.

### The ad images in Cloudflare R2

The images the campaigns post were the one thing in `workspace/` that only a
deploy could change: 17 files, 11 MB, baked into the Docker image and rsynced
onto the volume on every boot. Swapping a picture cost a commit, a PR, CI, a
build and a gateway restart -- the riskiest routine operation this bot had.

The bytes live in a Cloudflare R2 bucket, read over plain public HTTPS, and the
index of them lives in a Firestore `assets` collection beside the operator
config. `workspace/lib/ad_assets.py` is the only thing that reads either, and
R2 is the only source: nothing is baked any more, so a picture that cannot be
fetched is a failed run rather than a silent fallback to stale artwork.
`GENAIE_AD_ASSETS_BASE_URL` is the bucket's public origin and
`GENAIE_AD_ASSETS_SOURCE` selects nothing (it was the cutover switch).

One hard-won detail about that endpoint: **`r2.dev` answers `403` to a request
whose `User-Agent` is urllib's default `Python-urllib/3.x`**, and `200` to any
other value, an empty one included. Measured against the live bucket: `curl`, a
named agent and no agent at all all succeed; `Python-urllib/3.13` does not. So
`ad_assets` sends `User-Agent: genaebot-ad-assets/1`. Without it every fetch
fails and, now that there is no baked image to fall back to, the run exits
non-zero with nothing sent. A test pins the header.

That base URL is the bucket's `r2.dev` public development URL. Cloudflare
rate-limits those and says they are not for production traffic, which is
survivable here only because of the cache: the 17 objects are fetched once each
and then served off the volume for the life of that volume, so a normal week
makes no requests at all. If the cache is ever being refilled often -- or a
second bucket's worth of images arrives -- put a custom domain in front of the
bucket and change the one variable. Nothing else moves, because the URL is read
from the environment and never from the index.

The operator console reads this index too, through `ad_assets.catalogue` -- a
read that describes the pictures instead of fetching them, never raises, and
prefers a recent snapshot so the console cannot put a Firestore round trip
behind every page refresh. It is the only other caller, and the URL it hands out
is built by `ad_assets.public_url`, the same function `ensure` fetches with, so
a console cannot show one object while a run sends another. An unset
`GENAIE_AD_ASSETS_BASE_URL` costs the picture tiles and nothing else -- a
campaigns screen that will not load is worse than one that says why it has no
thumbnails. See *Operator Console API*.

`ad_assets.ensure` is called once per picture, before the first send, and hands
back a **local path** -- so the rotation, the pre-flight existence check, the
caption machinery and `send_ad` all work on files. The campaign's own `images`
list decides which pictures a run fetches and in what order, which is what makes
*adding* one an index edit rather than a code change.

`openclaw message send` would accept an `https://` URL for `--media` and fetch
it itself. It is deliberately not used that way. `send_ad` runs **once per
group**, so a URL would refetch the same two megabytes for every group in the
run and give every group its own chance to fail -- mid-run, where the engine
marks a group sent rather than retry an uncertain send. Downloading once and
verifying the digest proves the bytes *before the first send*.

The cache is at `media/ad-assets/<slug>/<sha256>-<filename>` on the volume, and
that location is load-bearing twice over: openclaw reads outbound local media
only from an allowlist of roots that includes `<state>/media` and does **not**
include `<state>/runtime`, and `media/` is outside the `workspace/` tree the
entrypoint wipes on every boot. So the cache survives deploys, and it is cold
only once.

Object keys are content-addressed -- `ads/<slug>/<sha256>.<ext>` -- so an object
is immutable, no edge cache can serve stale bytes, and a cached file's name
proves its contents. The digest is re-checked on every run; a few milliseconds
against broadcasting a corrupt image to thirty groups.

Failure falls back to the cache, not closed, because the cache only engages when
the targeting is good and R2 is not -- and posting a valid cached ad beats
posting nothing. Firestore unreachable serves the last-known-good index
snapshot, bounded at 48 h like `operator_config`'s own. A data-defined campaign
fetches **every** picture its document names before it sends anything, all or
none, so a dead object costs that campaign the day rather than silently changing
its rotation: the run exits non-zero naming the missing picture, and the cron's
failure alert fires.

To put a new picture live, from a checkout, with R2 credentials in the
environment and a Firestore key with Cloud Datastore User:

```bash
deploy/upload-ad-image.py cv ~/new-cv-ad.png --replace cv-ad.png   # dry run
deploy/upload-ad-image.py cv ~/new-cv-ad.png --replace cv-ad.png --write
deploy/upload-ad-image.py --verify                        # index vs the bucket
```

Credentials come from `~/.config/genaie/r2-upload.env` (`--env-file` points
elsewhere, `--no-env-file` uses the environment only); anything already exported
wins. That path is outside every checkout on purpose. This repository is
default-deny **except `deploy/**`, which is un-ignored wholesale**, so a file like
`deploy/r2.env` would be staged by `git add -A` -- and `deploy/ci/check.py` only
refuses tracked names *beginning* with `.env`, so it would not catch that one.
The file must be mode 600; the script refuses to read one other accounts can see,
and parses it as plain `KEY=VALUE` rather than sourcing it, so a credentials file
can never run anything.

It validates the file (JPEG or PNG by magic bytes, inside the gateway's 6 MB
image limit), uploads it, and writes the index document **and** the campaign's
own `images` list in one transaction -- a run attaches the pictures the campaign
document names, so writing only the index would upload a swap that never went
out. Nothing in the repository changes: the index and the bytes both live
outside it now. The R2 **write** credentials never enter the container and are
not Railway service variables: only the operator's machine signs anything, and
the gateway only ever makes unauthenticated GETs.

`--verify` is the parity gate. It used to diff the index against
`deploy/ad-assets.json`, a committed mirror of it, and against the baked
pictures the mirror was seeded from; all of that is gone, so it now checks the
pair that is actually load-bearing -- every picture the index names must be an
object in the bucket, because a run fetches all of them before it posts
anything. An entry with no object is reported as `DRIFT` and fails; an object no
entry names is reported as `SPARE` and does not, since that is expected for a
while after a picture is replaced.

### Per-group daily ad caps

How many ads a group got used to be an accident of how many promos listed it: a
group in six of the eight campaigns took six ads a day whatever its appetite.
Each group now has a **daily allowance**:

- the operator's `activity` tier picks a band -- `busy` 6-10, `normal` 4-6,
  `quiet` 1-3 (`defaults.bands`, and `daily_cap` on a group overrides the band
  outright);
- the **ad-reply signal the bot already records** moves the cap inside that
  band. `runtime/public-service-gate/ad-refs.json` counts inbound DMs carrying an
  ad ref, and a ref's suffix is a pure function of the group JID, so replies are
  attributable per group: one weighted unique replier earns one ad a day above
  the floor, decaying as the newest reply ages (full weight ≤7 days, half ≤14,
  a quarter ≤30, nothing after). That is why the cap moves day to day. A cap
  moves at most ±1 per day, so allowances drift instead of whipsawing.

Note what the signal is *not*: group chatter. Group events are dropped before
anything counts them, so there is no per-group message volume to read -- the tier
is the operator's judgement of that, and the replies modulate it. The
appliance-repairs ad contributes nothing either, because its `AR` prefix is
deliberately absent from the plugin's `AD_REF_PREFIXES` (that link points at the
repair shop's own number, so those replies never reach the bot).

When a group's cap is smaller than the number of campaigns targeting it,
`workspace/lib/promo_caps.py` picks the day's winners by **rotation**: a window
of `cap` entries over the group's fixed circular order, sliding by one per day,
so across one cycle every campaign gets exactly `cap` turns and none starves. It
is a pure function of `(day, jid, promo set, cap)` -- identical in every cron
process, needing no shared state and no lock, which matters because
`json_store` is last-writer-wins and the eight crons can overlap.
A switched-off campaign takes no turns, so pausing one hands its slots to the
campaigns still running instead of leaving them empty; a manual run or
`--dry-run` of the paused campaign still sees its own rotation. Which campaigns
are switched off comes from the resolved config the run has already read, so the
caps never guess at it -- and `promo_caps` deliberately has no way to work it
out for itself.

Caps are **on** (`defaults.caps_enabled` is `true` in `settings/defaults`), and
the engine fails open: a config it cannot read logs a warning and sends to every
eligible group. Preview the allowances with
`./workspace/bin/list-groups --caps` (and `--date YYYY-MM-DD` for another day) or
a promo's `--dry-run`, which prints each group's tier, cap, reply score and the
campaigns today's rotation gives the allowance to. With every group left at the
`normal` default and no reply history, turning caps on cuts the daily total by
roughly 15%: the seven daily lists hold 99 sends, and a measured Friday (when
the Mon/Wed/Fri weekly group posts) goes from 101 ads to 86. The band *floor*
(4) is what a group with no replies gets, so the groups in five and six
campaigns are the ones that come down.

### Promo caption rotation and ad ref tracking

The shared promo engine (`workspace/lib/promo_broadcast.py`) accepts either one
caption string (sent verbatim, as before) or a list of caption variants. Each
promo supplies three caption variants, except advertiser recruitment's single
fixed caption. Rewordings keep the advertised facts identical.
A group's variant is `(post counter + sha256(group JID)) % <variant count>`, so a
group never gets the same caption
on two consecutive posts (weekly-limited groups count their own posting days) and
groups are spread across variants on a given day. A promo may pass
`variant_images={variant_index: image_path}` to pin a fixed image to one variant
while other variants keep the daily image rotation. `--dry-run` shows each group's variant, ref and any pinned
image.

Each promo also passes a `TRACKING` config (opt-in; a promo without one sends
exactly its caption). Every group gets a stable ref code: a promo prefix plus
three characters derived from the group JID, e.g. `BH-VD4`. The caption is sent
with an extra line `Tap to chat on WhatsApp: https://wa.me/<number>?text=...`
whose prefilled text reads `Hi, I saw your borehole ad (ref BH-VD4)`. A caption
may instead place the link itself with the `{ref_link}` / `{ref}` placeholders.

| Promo | Prefix | Link target |
|---|---|---|
| appliance-repairs-promo | AR | +263713911390 (the repair shop, pinned) |
| borehole-drilling-promo | BH | the contact account (bot) |
| building-materials-promo | BM | the contact account (bot) |
| cv-promo | CV | the contact account (bot) |
| fruit-trees-promo | FT | the contact account (bot) |
| genaie-leads-promo | GL | the contact account (bot) |
| genaie-promo | GN | the contact account (bot) |
| skylight-solar-promo | SK | the contact account (bot) |

The bot advertises from two WhatsApp lines so neither account is a member of a
suspiciously large number of groups. The `lines/<id>` documents map each account
to the number it advertises and each group document names the account
that joined it (`account`, absent means the `default` line); a group can only be
posted to by the line that is actually a member of it. Public enquiries use
`defaults.contact_account` (`default`): both `{bot_number}` in the copy and the
`wa.me` link advertise **+263785632943**, whichever account sends the post.
It was `ads` (+263790453292) until that line was logged out of the gateway on
2026-10-29. Keep contact routing on default throughout the broadcast-only trial:
ads does not process customer DMs even after it is relinked.
The account-number map still records each line's actual identity. An explicit
third-party `TRACKING.number` (appliance-repairs) takes precedence. Without
`contact_account`, the engine retains the original per-sender contact behavior.
Default-line conversations, admin/self-chat, alerts and payment details are
unchanged. Contact routing is `settings/defaults.contact_account`, so changing
it takes effect on the next run with no deploy.

| Account | Number | Role |
|---|---|---|
| `default` | +263785632943 | the original line; also the admin/self-chat number |
| `ads` | +263790453292 | second line, added to share the group load; live and enabled since the sibling-line guard shipped |

### Advertising stability trial

The ads line has **15 groups configured for advertising and 45 campaign/group combinations**.
`let's build bindura` and `BULAWAYO ADVERTS` are active again for fruit trees,
building materials and Genaie advertisers. Zim Market Hub and ZiMarket Grp41
each receive appliance repairs, industrial lighting and building materials.
MAGWEGWE DEALS buy&sell group 1 receives borehole drilling, building materials,
Genaie advertisers and industrial lighting.
These are configured eligibility counts, before caps and weekly rules,
not confirmed deliveries.

The next round is **ads group advertising without automated DM replies**.
The tracked account override `channels.whatsapp.accounts.ads.dmPolicy: "disabled"`
is enforced on every boot, while `ads.enabled` remains operator-controlled.
The adapter's native inbound gate drops ads DMs before the agent, typing,
service actions and enquiry-triggered admin alerts. It sends no pause notice or
pairing challenge. This covers customer, administrator, self-chat and sibling-line
DMs. Default continues to handle customer conversations and admin alerts; alerts
explicitly send through account `default`. Group replies remain disabled on both
accounts. The ads phone stays on regular WhatsApp for this round; default uses
WhatsApp Business. Client identity is unchanged. Reply timing is now paced on
both lines (see **Reply pacing** below); pacing does not re-open ads DMs.

Each campaign reads gateway account status immediately before sending. Targets
for an offline or unknown account are deferred without submitting them or marking
them sent; other connected accounts continue. There is no automatic catch-up.
If a line drops mid-run, the campaign polls its status every 5 s for up to 90 s
(`PROMO_RECONNECT_GRACE_SECONDS`, 0 disables) before deferring that line's
remaining groups, so a routine status-500 relink no longer costs the day's ads.
The wait is shortened when the groups still to send would otherwise not fit
inside the cron job's timeout.
An already-submitted request with an uncertain result keeps its existing duplicate
protection. Campaign SDK connections are clients of the gateway, not additional
WhatsApp sessions.

A private, bounded lifecycle journal at `runtime/whatsapp-health/events-YYYY-MM-DD.jsonl`
records account, deployment, connection, event and reason, without messages,
recipients or credentials. It distinguishes watchdog recovery, transport errors,
logged-out authentication, conflicts and planned process stops. It retains 14 UTC
days, capped at 1 MiB per day; `GENAIE_WHATSAPP_HEALTH_AUDIT=0` disables writing.
Railway uses zero deployment overlap and a 360-second graceful drain. Nightly
standby remains 17:00–05:45 Africa/Algiers.

Deploy and verify the ads DM policy before one operator-assisted relink if its
credentials are invalid. Start the seven-day observation only once ads is
authenticated and broadcasts can run. Record connection events and campaign
receipts alongside any phone restriction notice. Record manual replies, device
links and deployments as confounders; avoid discretionary reconnects or campaign
changes. An offline day does not establish restriction-free advertising.
Replies stay disabled after seven days until an explicit decision to resume them.
To resume, change the tracked ads `dmPolicy` to `"open"` and deploy; a valid
session does not need another relink just to change that policy.

### Reply pacing

WhatsApp cannot read a reply, but it can see how a line behaves. The ads line was
restricted while it answered customer DMs the way the gateway does by default:
"typing" the instant a message landed, the reply a few seconds later whatever its
length, canned replies (the CV interview) in milliseconds, each bubble of a burst
answered separately, and a night's backlog answered in one second at wake-up.

Customer replies on **both lines** are now held for as long as a person would take
(`workspace/plugins/public-service-gate/lib/reply-pacing.js`):

| Step | Wait |
|---|---|
| Before starting on a message in a quiet chat (no exchange for 5 min) | 5 to 25 s, mostly toward the short end |
| Before starting on a message in an active chat | 2 to 8 s |
| Typing the reply | its length at 4 to 8 characters a second, 1.5 to 30 s, less the time the model took |
| Moving to a different customer's chat on the same line | 2 to 6 s after the previous reply |

A first reply lands after roughly 8 to 40 s and a follow-up after 4 to 20 s. No
reply is held longer than 90 s, and any failure sends it at once: a late reply is
acceptable, a lost one is not. Only a chat whose customer wrote in the last 10
minutes is paced, so promo broadcasts, admin alerts, partner forwards and CV
deliveries are untouched, as are the operator's own chat, the sibling line and
groups.

Two template values go with it and are forced into the live config on every boot
by `sync-reply-gates.py`: `agents.defaults.typingMode: "message"` ("typing" shows
when the reply text starts, after the read pause) and
`messages.inbound.byChannel.whatsapp: 5000` (a burst of short messages becomes one
turn). The outbound hook registers a 120 s budget because OpenClaw 2026.9.9 gives
`message_sending` handlers 15 s by default; a handler that runs out of budget is
skipped and the reply is sent.

Set `GENAIE_REPLY_PACING=0` in the Railway variables to switch the waits off; the
two template values stay. Known gaps: "typing" is not refreshed once the model has
finished, so on a long hold it can lapse before the message arrives; read receipts
stay off; the linked device still registers as the OpenClaw client. Pacing removes
the timing signature. It does not make an unofficial client sanctioned, and it is
not a guarantee against restriction.

### The two lines talking to each other

When both lines have DM replies enabled, one DMing the other can make them answer
each other forever: on 2026-10-20 a single test message produced an endless "Silent." /
"👌" exchange until one account was disabled. Nothing upstream prevents it.
OpenClaw's pair loop protection needs a reliable inbound bot identity
(`allowBots`), which a WhatsApp DM cannot provide, so WhatsApp is not a supported
channel for it and `botLoopProtection` is rejected by the strict WhatsApp config
schema. There is no deny list for WhatsApp DMs either, and an allowlist is
unusable because an advertising line must accept DMs from strangers.

To stop a loop like that again, set `REPLIES_DISABLED=1` in the Railway variables:
it forces `channels.whatsapp.accounts.default.enabled` to `false` on every boot and
is reversible by deleting the variable. Silencing the `default` line is enough to
break an own-lines ping-pong, because one side going quiet ends the exchange — but
the switch governs **only** the `default` line, so a loop or flood that involves the
`ads` line and an outside party is not stopped by it.

The `ads` line has no equivalent tracked switch on Railway. Nothing on the boot path
applies `config/openclaw.template.json` (see **Deploying Changes**), and
`sync-reply-gates.py` deliberately never writes `accounts.ads.enabled`, so editing
the template does **not** reach the live config here — it only matters for a Compose
deploy or a fresh seed. To silence the `ads` line on Railway, hand-edit the live
`openclaw.json` on the volume (`accounts.ads.enabled` to `false`); that edit is *not*
undone on the next deploy, because the reply-gate sync only forces its tracked
scalars (and appends the `allowFrom` wildcard) and leaves everything else — the `ads`
account, `groupPolicy`, `bindings`, every `PRESERVE_PATHS` entry — alone.

The lines are allowed to talk, on a budget enforced by
`workspace/plugins/public-service-gate/lib/sibling-lines.js`: **8 messages per
15-minute window, then a 6-hour cooldown, at most 3 bursts per UTC day**
(`siblingLine*` options, named after core's `botLoopProtection`). Under the
budget a cross-line DM is answered normally; over it the message is claimed
silently, so there is no reply and no model spend.

The rule that makes this safe: a DM is held only when the sender is one of our
own line numbers **and** is not the number of the account that received it. The
admin number is also the `default` line's number, so admin self-chat is never
caught. Telling the two apart needs the receiving account id, which comes from
the `before_dispatch` context (`getEventAccountId`); when it or `whatsappLines`
is missing the guard fails open and warns, because wrongly silencing a customer
or the operator is worse than a loop. A sibling line is also forced to
non-admin, so one line can never run the other's admin commands.

Per-pair counters and a rolling transcript of what the lines said live in
`runtime/public-service-gate/sibling-lines.json` (agent session transcripts reset
daily at 04:00 and prune at 30d, so this is the durable record). Suppressions log
one `public-service-gate: sibling-line DM held` line.

**Never use one line to test the other** - that is what started the loop. Verify
a line with `openclaw channels status`, or send from a third number. Over
`railway ssh` that command needs `env -u OPENCLAW_HOME` (see "Operational
Changes"):

```bash
railway ssh -- env -u OPENCLAW_HOME runuser -u joseph -- openclaw channels status
```

Decode refs with `--refs`, which prints `ref  promo  group_jid  link` for every
target and exits without sending or touching state:

```bash
railway ssh -- runuser -u joseph -- \
  /home/joseph/.openclaw/workspace/bin/run-console-campaign borehole-drilling --refs
```

When a DM carrying a ref reaches the bot, the public-service-gate plugin counts it
in `runtime/public-service-gate/ad-refs.json` (`count`, `uniqueSenders`,
`firstSeen`, `lastSeen` per ref; senders stored only as a short one-way hash).
Recording is observe-only and never changes the reply; admin DMs and group
messages are not counted. Every promo except `appliance-repairs-promo` links to
one of the bot's own lines, so every reply is counted and answered by the bot
whichever line it arrives on (both accounts route through the same bindings and
the same ad-ref store), and the bot forwards partner orders (fruit trees,
building materials) to the partner when it hands the customer off. The
appliance-repairs link goes to the repair shop's own
number with the prefilled text `Saw your post from Genaie ads (ref AR-XXX)`, so
those replies never reach the bot and are not counted here; the shop sees the ref.
Keep the prefix list in `workspace/plugins/public-service-gate/lib/ad-refs.js` in
sync with the `TRACKING` prefixes of the promos that link to the bot (a test
enforces this).

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

There are two exceptions. The first is the public WhatsApp system prompt at
`workspace/docs/public-whatsapp-system-prompt.txt`. On every boot, after the
OpenClaw upgrade step and before the gateway starts, the entrypoint runs
`deploy/railway/sync-public-prompt.py`, which writes that file into
`channels.whatsapp.direct["*"].systemPrompt` and touches nothing else in the
config (peer overrides included). It logs `public WhatsApp prompt updated` or
`unchanged`; any problem (missing or empty file, bad JSON) is logged as
`NOT synced` and the boot carries on with the previous prompt. So a prompt edit
reaches the live agent on the next deploy, and the business facts it reads
(`workspace/docs/business-info.md`) are synced with the rest of `workspace/`.

The second exception is the WhatsApp **reply gates**. Right after the prompt sync
the entrypoint runs `deploy/railway/sync-reply-gates.py`, which sets each of these
nine keys to its value in `config/openclaw.template.json`:

| Path | Template |
|---|---|
| `channels.whatsapp.enabled` | `true` |
| `channels.whatsapp.dmPolicy` | `"open"` |
| `channels.whatsapp.accounts.default.enabled` | `true` |
| `plugins.entries.whatsapp.enabled` | `true` |
| `plugins.entries["public-service-gate"].enabled` | `true` |
| `plugins.entries["public-service-gate"].config.enabled` | `true` |
| `plugins.entries["whatsapp-relevance-gate"].config.enabled` | `false` |
| `plugins.entries["whatsapp-relevance-gate"].config.relevanceMode` | `"off"` |
| `plugins.entries["public-service-gate"].config.cvMakerPaymentDetails` | `"EcoCash +263775489417 (Noriah Mzenda)"` |

The last one is not a reply gate: it is where CV buyers are told to pay, governed
so a change of payee reaches the live bot on the next deploy.

It writes **scalar leaves only** -- no object or array is ever replaced -- so the
live-only plugin keys the strict `configSchema` still requires survive untouched.
That matters: replacing a config subtree is exactly what crashlooped production on
2026-10-12 (see the warning below).

`dmPolicy` and `allowFrom` are coupled: `dmPolicy="open"` drops **every** DM
unless `channels.whatsapp.allowFrom` contains `"*"`, so the script appends `"*"`
when it is missing. That is the one write that is not a scalar leaf, and it is an
append -- `allowFrom` holds admin and public phone numbers and is never replaced
or reordered.

Deliberately **not** governed:

- `bindings` -- the template's two entries omit the admin->`main` routes that
  `deploy/patch-openclaw-config.py` generates from `whatsappAdminUsers` at apply
  time, so forcing it would delete admin routing and land the operator's own DMs
  on the restricted `public` agent. The script only *checks* that a binding routes
  direct WhatsApp peers to `public`, and logs a `WARNING` if none does.
- `channels.whatsapp.accounts.ads.enabled` -- a line that is legitimately on must
  not be silenced by a deploy.
- `channels.whatsapp.groupPolicy` -- group replies are hard-blocked in
  `workspace/plugins/public-service-gate/index.js`, and this script only ever
  turns replies *on*.
- every `PRESERVE_PATHS` entry -- runtime-derived or PII, absent from the template
  by design.

Railway also applies the optional-plugin enablement policy with
`deploy/railway/sync-runtime-plugins.py`. It writes only the eleven optional
plugin `enabled` leaves declared by that script, retaining live plugin options,
WhatsApp routing, both accounts, model access, memory, file transfer and local
execution. Browser, canvas, computer/device, geolocation, GitHub, Ollama, voice,
Anthropic and xAI integrations are disabled to reduce the gateway footprint.
Changing a governed plugin's template boolean restores it on the next deploy.
See [Railway cost measurement](deploy/railway/README.md#cost-measurement) for
baseline comparisons, the separate allocator experiment and the $8 usage alert.

The reply-gate sync logs `reply gates unchanged (12 paths already match the template)`,
`reply gates updated: <path> <old> -> <new>; ...`, or, on any problem,
`reply gates NOT synced: <err>` and the boot carries on with the config unchanged.
A non-fatal `openclaw config validate` runs straight after and only warns, because
a fatal one here would turn a config nit into a crashloop.

**To turn replies off, set the Railway variable `REPLIES_DISABLED=1`** and
redeploy; delete the variable to restore them. Both states are enforced on every
boot, so the switch is visible in the Railway dashboard. **Do not hand-edit
`openclaw.json` on the volume to disable replies** -- the next deploy restores
them, and an edit there leaves no trace in git. That is exactly what happened
during the 2026-10-18 outage: `dmPolicy` was set to `"allowlist"` with only the
admin number in `allowFrom`, so the bot answered the operator and nobody else, and
nothing in the repository recorded it.

`sync-reply-gates.py --dry-run` reports what it would change and writes nothing,
which is the quickest way to diff the live config against the template:

```bash
railway ssh -- python3 /usr/local/bin/sync-reply-gates.py \
  /opt/live-source/config/openclaw.template.json \
  /home/joseph/.openclaw/openclaw.json --dry-run
```

Right before `openclaw gateway` starts, the entrypoint also runs
`deploy/railway/wait-gateway-lease.py`. A container killed mid-drain (for example a
deploy that lands while a promo cron run is sending) leaves its gateway owner lease
in `state/openclaw.sqlite` for up to its 300 s TTL, and the next container (a new
hostname) cannot reclaim it, so the gateway refuses to start and Railway's 10 quick
restarts run out (2026-10-18 outage). The script waits for such a lease to expire,
at most 330 s, then lets the gateway start; it is never fatal. Avoid merging to
`main` during promo windows anyway: a deploy cuts off the run in progress.

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

## Firestore read efficiency

Console displays cache validated configuration for 180 seconds across bridge
processes. Each campaign scope has its own cache and bounded outage snapshot;
scoped reads never overwrite a full snapshot. Failures suppress background
retries for at least 300 seconds, respecting longer upstream retry delays.
Mutation markers and API generations fence older in-flight reads after writes.
Save checks use `fresh=1` (`--fresh-config` locally) and fail if a live read fails.

Campaign runs and previews query only groups assigned through `promos`, which
also covers valid weekly and paused assignments. The same shared rules and cap
calculation apply. Existing group edits/deletions use point reads plus shared
validation dependencies. Campaign creation reads only its selected groups;
switch and caption save checks read one campaign document. Settings reads omit groups and campaign definitions;
capability reports read no database documents. Sidebar counters use Firestore
aggregations. The overview does not fetch the picture index. Image resolution
queries only assets belonging to the current campaign.

New authenticated read routes are `campaign/<slug>`, `campaign-groups/<slug>`,
`campaign-state/<slug>`, `captions/<slug>`, `group/<jid>`, `groups-by-id/<comma-separated-jids>`, `settings`,
`capabilities`, and `navigation`. Existing routes remain available. Group IDs
retain both modern and legacy WhatsApp formats.

`POST /internal/console/config/batch/groups` accepts `changes` (1..100 existing
groups, each with `id`, `fields`, and optional expected values for changed
fields), `actor`, and optional `dryRun`. It reads shared rules once inside a
Firestore transaction, validates every change, and commits changed groups and
per-group audits together. A refusal, conflict or dry run writes nothing;
unchanged groups have no audit/write. An uncertain commit is reported as unknown
and must be read back before manually retrying.

Schedule reconciliation queries only `kind=console` campaigns every five
minutes, increasing the delay after failures up to one hour. Campaign edits wake
it immediately. Already-current schedule statuses are not rewritten. Broad
configuration reads remain for complete list/overview screens, creation rank
allocation, and validation of shared changes that can affect other groups.

## Operator Console API

`deploy/railway/console-api.py` serves the operator console's read model over
HTTP beside the gateway. Campaign and membership responses come from
`workspace/bin/console-read`, which reads the Firestore operator config, the
picture index, and the ad-reply counters the caps derive from. It never writes
to the repository, never contacts the gateway, and never records a cap snapshot,
so reading it cannot move the rotation on. The one thing a read
does leave behind is the last-known-good Firestore snapshot on the state volume,
refreshed exactly as a campaign run refreshes it. Separate account endpoints contact the
running gateway for live status and account controls, and one write route
changes operator config in Firestore (*Changing operator config from the
console*, below).

Read routes, GET or HEAD, under `/internal/console/`:

| Route | Returns |
|---|---|
| `health` | `{"ok": true}` and nothing else; needs no token |
| `overview` | group counts by status, today's planned sends, the lines, every campaign |
| `groups` | one record per known group: status, line, tier, cap, campaigns, note |
| `campaigns` | one record per declared promo: slot, captions, tracking, reach |
| `preview/<slug>` | per group, the caption variant, ref code and link that campaign would send |

Each data route takes an optional `?date=YYYY-MM-DD`. Anything else is refused:
an unknown path or an unusable slug is 404, an unparseable date or an unexpected
query parameter is 400. These read routes reject write methods and request bodies.

The `campaigns` payload also carries **what each campaign's pictures are and
where their bytes are**, so the console can show them rather than name them:

```json
{"date": "...", "adAssets": {"mode": "repo", "source": "firestore",
                             "fetchedAt": "...Z", "note": null, ...},
 "campaigns": [{"slug": "borehole-drilling",
                "images": ["borehole-1.jpg", "..."],
                "assets": [{"filename": "borehole-1.jpg",
                            "url": "https://pub-....r2.dev/ads/borehole-drilling/8afa....jpg",
                            "sha256": "8afa...", "bytes": 243016,
                            "contentType": "image/jpeg", "rank": 10}]}]}
```

`images` is what the campaign's own document declares, which is what a run
attaches; `assets` is what the Firestore picture index says, in rotation order,
joined by `filename`. They are reported separately rather than merged because
they are two different claims, and the day they disagree is worth seeing --
`deploy/upload-ad-image.py` writes both in one transaction precisely so they
cannot. `url` is `null` when `GENAIE_AD_ASSETS_BASE_URL`
is unset, which is not the same as having no pictures.

An index that cannot be read costs the pictures and nothing else: `assets` is
`[]`, `adAssets.note` says why, and the campaigns payload is otherwise complete.
`adAssets.mode` is now always `r2`, since there is no other source for a run to
attach, and `adAssets.source` says whether the index came from Firestore or from
the snapshot.

There used to be one more rung: `deploy/ad-assets.json`, a committed mirror of
the index, baked into the image. It is gone with the rest of the operator data
in this repository, and it was not theoretical -- the first deploy of this
feature showed every tile blank, because the project's Firestore quota was
exhausted (`429 RESOURCE_EXHAUSTED`) **and** no run had yet written a snapshot,
so nothing anywhere said where the pictures were while the bytes sat in the
bucket. What covers that case now is the index snapshot, which every run and
every console read refreshes, plus `deploy/backup-operator-data.py` as the
copy of record.

A live read that fails also writes `runtime/firestore/ad-assets-unreadable.json`
and is not retried for `RETRY_AFTER_SECONDS`. A console tab refreshes every 60
seconds, and retrying an exhausted quota on that loop is both useless and part
of what keeps it exhausted. One attempt per window still notices the quota
coming back.

Reading it is snapshot-first (`ad_assets.catalogue`, bounded by
`CATALOGUE_MAX_AGE_SECONDS`, 5 minutes). A console tab refreshes every 60
seconds and the bridge is a fresh process each time, so without that bound every
refresh would be a token fetch and a `list_documents` on a project already
answering 429 for the config read. A live read rewrites the snapshot, so the
window keeps refilling itself -- and in `repo` mode, where no run ever touches
the index, a console read is the only thing that creates that snapshot at all.

The `groups` payload also includes `discoveredGroups` (unconfigured metadata
observations, deduplicated by JID, with per-account observation times) and
`groupDiscovery` (`enabled` plus per-account scan status, last attempt, last
success and a sanitized error code). These are separate from `groups`: they
are not targets, do not affect caps or counts, and cannot resume a paused group.
Reading the API only reads saved observations; it never triggers a WhatsApp scan.

Configured and discovered rows also carry read-only `metrics`: rolling
`dailyMessageCount` (incoming messages over the last 24 hours), `windowStart`,
`asOf`, `coverage` (`complete`, `partial`, `unavailable`), `memberCount` and
`memberCountObservedAt`. Counts are nullable; known zero is distinct from missing
data. The metadata scan supplies the newest member total. The independent socket
collector excludes bot posts, edits, reactions, protocol events and history sync,
and deduplicates group/message IDs across lines and reconnects. It stores only
minimal event metadata and hashed senders in a separate durable SQLite database,
with 48-hour event/coverage retention. It never affects tiers, caps or targeting.
Counts are partial during warm-up, downtime and collection failures. Collection
starts at release with no history backfill. Metrics remain live even when
`date` asks for a historical campaign plan.

Deploy this bot support before the paired console UI, which shows DMC and
Members and moves notes under **More**. Older consoles ignore the added fields;
the new console shows unavailable metrics with an older bot. See
[group activity operations](deploy/railway/README.md#group-activity-and-member-totals)
for storage, coverage, opt-out and failure behavior.

### Changing operator config from the console

One write route, so the console can pause a group or switch a campaign off
without a deploy:

```
POST /internal/console/config/<collection>/<id>
     {"fields": {"status": "on-hold"}, "actor": "joe", "dryRun": false}
     {"delete": true, "reason": "bot was removed", "actor": "joe"}   # groups only
```

`<collection>` is `groups`, `lines`, `campaigns` or `settings`; `<id>` is
percent-encoded, because a group JID carries an `@`. It writes **Firestore**,
which production has read since the cutover (*Moving membership to Firestore*);
`overview.writes.affectsProduction` says whether that is the case. A change takes
effect from each campaign's **next run** -- a run already sending keeps the
config it resolved when it started.

Console displays use `workspace/lib/console_config.py` to share a three-minute
config cache between read bridge processes. A volume lock shares each refresh;
cached data is scoped to the credential/project context and API generation.
After a failed refresh, background display reads wait five minutes before trying
again. The existing validated fallback snapshot retains its original timestamp,
failure reason and 48-hour limit. Metrics and discovery keep their current cadence.

An authenticated read with `fresh=1`, or `console-read --fresh-config`, bypasses
both the API response cache and config display cache and requires a live read.
Save preflights use this path; writers and campaign sends retain their live
checks. Config changes and campaign creation invalidate the display cache before
and after the attempt, including failures and unknown outcomes. Generation checks
prevent older in-flight responses from restoring an invalidated cache. Schedule
reconciliation invalidates config only when it actually writes schedule status.

Firestore errors expose optional curated `code`, `upstreamStatus`, `quota`
(`daily`, `rate`, or `unknown`) and `retryAfterSeconds` fields. A config read 429
has `code: config_rate_limited`; a refused write retains HTTP 503 and `ok: false`.
Only numeric delays and quota classifications are copied from Google's response,
never its arbitrary message or credential details. Identified daily quota failures
are not immediately retried. Deploy this backend before the console's fresh-read
save preflights; no quota or billing settings change.

Every change goes through `workspace/bin/console-write`, which is to writes what
`console-read` is to reads: a separate process importing the bot's own modules
from the volume. The sequence, in `workspace/lib/operator_writer.py`, is the
point of the whole design:

1. read every collection live, exactly as a campaign run does;
2. apply the patch to **one** document, field by field, names checked against an
   allowlist per collection;
3. re-assemble the whole config and run it through `operator_config.quarantine`,
   which is `promo_membership.validate` -- the same code that decides whether a
   run may post to a group;
4. refuse if the edited document would be quarantined; otherwise commit it.

Step 3 is why there is no second validator to drift. It also catches what a
per-field check cannot: pointing a group at a line that does not exist, or
clearing the `contact_account` every campaign's caption needs. A refusal says
what would have broken and changes nothing.

Step 3 has one blind spot, closed before it runs: values the reader is
**deliberately forgiving** about, and so never complains of. `caps_enabled` is read
with `bool()`, so the string `"false"` switches caps *on*; a malformed band quietly
falls back to the default band; a campaign switch that is not a boolean reads as
*off*. Forgiving is right when reading -- a capacity setting must never be why a
day's ads do not go out -- and wrong when writing, where it lets a save succeed and
then be ignored. The writer refuses those values, judging only the fields a change
names, so something already typed into the Firebase Console cannot block an
unrelated edit.

**Pausing one campaign for one group** is `paused_promos: [slug]` on the group. The
assignment in `promos` stays, so resuming is a flip; a paused campaign neither posts
to the group nor takes one of its daily cap turns (both go through
`promo_membership.groups_for_promo` / `promos_for_group`). Each paused slug must be
in `promos`. That makes **removing** an assignment a three-field write: the campaign
has to leave `promos`, `paused_promos` and `weekly` together, because any one left
behind names a campaign the group is no longer in, and the writer refuses it.

**Deleting** is for groups only. A line still named by groups would quarantine them
all, a campaign is code, copy, a cron and an installer run, and `settings` is what
every group is judged against. The whole deleted record goes into its audit document
in the same commit, so a group can be gone from the config but never from the trail.
Recreating one is a manual re-create from that record. The ordinary way out of the
rotation is still `status` or a pause, which keep the assignments.

A delete on its own leaves the bot in the WhatsApp group, so discovery lists it
again under `discoveredGroups`. The two things that finish the job are separate,
optional, and described under *Group actions* below.

What this deliberately will not do: edit `rank` (that is send order, so
reordering is its own operation -- a new group is appended last), write more than
one document per request, or create a line or a campaign (a new line needs a
WhatsApp login, a new campaign needs code, copy, a cron and an installer run).

### Group actions

Deleting a group's record says nothing about the WhatsApp group itself or about
what the bot stored while advertising to it. Two actions cover that, each asked
for on its own:

* **leave** -- take the account's number out of the WhatsApp group. Nothing in
  the console API or the cron tooling can do this: the gateway exposes no RPC for
  it, and a second process cannot borrow an account's socket. So the request is
  queued on the volume at
  `runtime/whatsapp-group-actions/<accountId>/requests/<uuid>.json`, and
  `whatsapp-group-actions.mjs` -- injected into the channel monitor beside the
  group inventory, holding the live socket -- performs it and writes the answer
  to `results/<uuid>.json`. A successful leave also prunes the group from that
  account's discovery inventory, because an inventory that still lists it would
  put the group straight back under `discoveredGroups`. A scan never removes a
  group that has gone -- it only merges what the lookup returned -- so a lookup
  that resolved before the leave would restore the entry for good; the prune is
  timestamped and the merge drops observations older than it. A group the
  account is not in reports as already done, not as a failure.
* **clearRecords** -- remove the group from every campaign's state in
  `runtime/cron-state/<name>/state.json`: its `sent` receipt, and any `resolved`
  invite-link cache entry that points at it (`resolved` is keyed by the link,
  with the jid as the value, so it is matched by value, not by key). The sweep
  reads and writes under the state file's lock, which `promo_broadcast` also
  takes when it saves, so a send committed while the sweep is waiting cannot be
  overwritten -- losing a receipt would let the next run post the same advert
  again. Locking fails open: a lock that cannot be taken never stops a send.
  This deliberately
  does **not** touch `ad-refs.json` or `ad-ref-history.json`: those are keyed by
  a ref code derived from the jid, are written live by the public-service-gate
  plugin, and are evidence somebody replied to an advert. Stale entries there are
  inert, because scoring only ever looks up the groups it is given.

Both go through `workspace/bin/console-group-action`, which holds no Firestore
credential of any kind, behind
`POST /internal/console/groups/<jid>/actions`. A leave whose answer has not
arrived within the bridge's wait is reported as **unconfirmed** rather than
failed -- the request is still queued and the account may already be out, and
telling an operator it failed would have them retry something that happened.
`GENAIE_GROUP_ACTIONS=0` switches the whole surface off: the route answers 503
and the monitor's helper stays inactive.

**What a console may offer** comes from the writer itself. `overview.writes` is
`{enabled, affectsProduction, fields, delete}`: whether the console API holds the
write key (checked by presence, never read), whether a saved change reaches the
campaigns, and `operator_writer.EDITABLE` / `DELETABLE` verbatim. Group actions
are reported separately, as `overview.actions`
(`{leaveGroup, clearRecords}`), because neither writes Firestore and gating them
on the write key would hide a working control behind a credential it does not
use. A console reading an older bot finds `actions` absent and must take that as
"cannot". A console gates
every control on that, so it cannot present an edit the bot will refuse.
`overview.capSettings` is `{enabled, defaultActivity, bands}`, the cap settings in
the shape a form edits them.

Each change is committed **together with its audit document** in one Firestore
commit -- `audit/<timestamp>-<random>`, carrying who, what, before and after.
Firestore keeps no history of a write and the Firebase Console bypasses this
path entirely, so that trail is the only one there is. The `audit` collection is
not one of the four a reader lists, so it costs a campaign run nothing.

Writes are serialised. `operator_writer` reads the config, applies a patch and
writes the whole document back, so two at once could each miss the other; one at
a time removes that for anything coming through the API. It cannot remove it for
a Firebase Console edit made at the same moment -- Firestore has no transaction
here -- which is the other reason the audit trail matters. A write also clears
the read cache, including when it fails, because an uncertain write may still
have landed.

**It ships inert.** Writes need `GCP_SERVICE_ACCOUNT_WRITE_B64`, a key with
Cloud Datastore User, and answer 503 without it. That key is deliberately *not*
in the entrypoint's `SECRET_KEYS`: that list is exported into the shell that
execs the gateway, so everything on it reaches every cron run, and a campaign run
must not be able to rewrite the config it is about to act on. The entrypoint
captures it, unsets it, and hands it to the console API process alone
(`deploy/test_console_write_deploy.py` pins that). The runtime key the cron runs
carry stays Cloud Datastore Viewer.

Because `console-read` imports the bot's own `promo_membership`, `promo_caps`
and `promo_broadcast` rather than reimplementing them, a preview is the same
text the cron would send. The variant index it reports is 0-based, exactly as
`caption_for` returns it; `run-console-campaign --dry-run` and the console both
add one only when displaying it.

A campaign that is switched off still has copy and still has groups configured
for it, but never posts. Every payload says so rather than leaving the console
to guess: `campaigns` and `preview/<slug>` carry `enabled`, `groups` carries a
`disabledPromos` list so a screen can tell "campaign is off" from "held back by
today's cap", and neither `allowedToday` nor `overview`'s `plannedSendsToday`
counts a send for one. There are two switches, and `campaigns` reports both
beside the effective `enabled`: `storeEnabled`, the operator's own switch in
Firestore and the one a console toggle edits, and `cronEnabled`, whether the
reconciler has confirmed the gateway actually has the cron
(`scheduleStatus == "ready"`). A toggle cannot fix the second, so a console must
not pretend it can. `preview/<slug>` lists paused assignments too, marked
`paused` with `sendsToday` false -- it is the list a pause is resumed from.

It ships disabled. Enabling it is environment only, and
`deploy/railway/README.md` has the recipe:
[Operator console API](deploy/railway/README.md#operator-console-api).

### WhatsApp account controls

The same bearer-authenticated API now exposes:

| Method | Route under `/internal/console/` | Body / result |
|---|---|---|
| GET | `accounts` | Live account state and observation time; no read cache |
| POST | `accounts` | `{"id":"sales"}` creates an account and QR session |
| POST | `accounts/<id>/link` | `{}` starts or resumes a QR session |
| GET | `accounts/link-sessions/<session-id>` | QR updates and pairing completion |
| POST | `accounts/<id>/pause` | `{}` disables and stops, retaining login |
| POST | `accounts/<id>/start` | `{}` enables and starts with saved login |
| POST | `accounts/<id>/unlink` | `{}` disables, stops and clears login |

Unlink retains configuration, group assignments, contact references and history.
Permanent removal is not implemented. New accounts have no assignments and set
both DM and group reply policies to disabled. Existing reply policies survive
all four controls. Account status merges the gateway runtime with saved login
presence/phone identity; no credentials or raw configuration are returned.

`deploy/railway/console_accounts.py` serializes access to one persistent
`workspace/lib/console_accounts.mjs` bridge. It uses the pinned gateway SDK and
RPCs, with conflict-checked `config.set` writes and no requested gateway restart.
A configuration still waiting to apply is reported rather than treated as active.
Unknown write outcomes are never replayed. Status is refused while a saved
configuration is not applied or its revision changes during a read; action
acknowledgements are checked against the final enabled and running state.
The bridge enforces its response deadline even after partial output, with a
1 MiB response limit. QR sessions expire after three minutes;
closing a console dialog stops polling, and Link resumes an active attempt.
Native pairing is checked before conflicting controls, including after API restart.

The default account's explicit enabled flag is now preserved on boot; absent
flags are initialized from the template. `REPLIES_DISABLED=1` still forces it off
and prevents Start/Link. After removing that kill switch, explicitly Start the
account to resume. Ads DM policy remains governed by the tracked template.
Already submitted messages may still arrive after Pause or Unlink.

Controls require the live API beside a running OpenClaw **2026.9.9** gateway and
its existing `GATEWAY_AUTH_TOKEN`. Local checkout-only reads cannot manage accounts.
During overnight standby operations report unavailable; they do not wake the gateway.
For a checkout server, `OPENCLAW_STATE_DIR` must point at the gateway state whose
`workspace/lib/console_accounts.mjs` contains this version. The installed-runtime CI runs the complete lifecycle against the pinned SDK
and tests redaction, conflict detection and hot configuration publication against
an isolated real gateway. No tests scan a real WhatsApp QR code or mutate a
production account.

Run it against this checkout the same way production does:

```sh
CONSOLE_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  CONSOLE_READ_BIN="$PWD/workspace/bin/console-read" \
  CONSOLE_API_BIND=127.0.0.1 \
  python3 deploy/railway/console-api.py
```

Or read the same payloads with no server at all:

```sh
./workspace/bin/console-read overview
./workspace/bin/console-read preview borehole-drilling --date 2026-11-04
```

## Operational Changes

### WhatsApp connected but not receiving direct messages

OpenClaw 2026.9.9 can discard an incoming DM before normal inbound logging when
the sender uses a WhatsApp LID and its phone mapping is missing. A healthy socket
does not prove reception works. The diagnostic signature is `LID mapping not
found ...; skipping inbound message` with `logging.level=debug`. This is distinct
from a 401 logout; restarting or repeatedly scanning a QR does not repair it.

Railway loads `deploy/railway/whatsapp-lid-loader.mjs` before starting the gateway.
It adapts the installed WhatsApp normalizer, including OpenClaw's plugin captures,
without editing vendor files. `whatsapp-lid-recovery.mjs` keeps account- and
domain-scoped routing hints in `runtime/whatsapp-identities/mappings.json`,
outside the relinked credentials. It uses the current account's mappings and
authenticated message-key alternates; it never borrows customer identities from
another account. Cached aliases alone cannot authorize a phone allowlist,
administrator, configured member or privileged agent binding. The schema-v1
cache's unscoped phone aliases are discarded when upgrading. Without a phone, a DM keeps its opaque
`<id>@lid` or `<id>@hosted.lid` identity and still reaches the public agent.
An account keeps an opaque identity stable when a verified, unambiguous mapping
links later phone-addressed messages to it. Unassociated or ambiguous identities
are never guessed or merged. Lookup failures/timeouts and malformed cache fields cannot
discard an otherwise addressable DM.
Opaque IDs stay distinct from phone numbers throughout lead, handoff, stop and CV
state; they never inherit phone-based administrator or sibling-line privileges.
Handoffs without a phone alert the operator to continue in the original chat.
CV orders record their receiving WhatsApp account; PDF and decline deliveries
use that account explicitly. Existing orders without an account belong to
`default`. An interview on one line does not capture messages sent to the other.
The adapter's target formatter is also patched: without this, it converts a LID
into an invented phone number, including when formatting command-owner checks.
Standalone CV delivery preloads the compatibility code itself and refuses an
opaque-ID send if the loader is unavailable. Other standalone `openclaw message`
commands targeting LIDs must set `NODE_OPTIONS=--import=/usr/local/lib/genaie/whatsapp-lid-loader.mjs`;
the gateway and its child processes already inherit that setting.

The loader checks the adapter's exact version and patch anchors; unsupported
versions or changed matched bundles fail visibly. CI checks that its supported
version matches the image pin. Test bundle names and the compatibility patch
before updating OpenClaw. Do not delete the identity archive during a
relink, or copy Signal session/key files between accounts. Verify reception with
an outside-number DM and its reply, rather than channel status alone.

### Gateway commands

Runtime and user operations go through approved commands rather than hand-edited
config. Shell into the live gateway with `railway ssh`:

```bash
railway ssh -- /home/joseph/.openclaw/workspace/bin/openclaw-cap --help
```

Example:

```bash
railway ssh -- /home/joseph/.openclaw/workspace/bin/openclaw-cap users list
```

`railway link` once in this checkout so `railway ssh` resolves the project,
service and environment (see `deploy/railway/README.md`).

**Running the `openclaw` CLI over `railway ssh` needs `env -u OPENCLAW_HOME`.**
The image sets `OPENCLAW_HOME=/home/joseph/.openclaw` (`deploy/railway/Dockerfile`),
but openclaw treats it as a *base* directory and appends `/.openclaw`, so the
config resolves to `.openclaw/.openclaw/openclaw.json` and the command fails with
`Config file not found`. `entrypoint.sh` unsets it for the gateway, and
`deploy/gateway-exec.sh` strips it for installer calls, but a `railway ssh`
session inherits the image value:

```bash
railway ssh -- env -u OPENCLAW_HOME runuser -u joseph -- openclaw config validate
```

The `openclaw-cap` commands above are unaffected — they resolve their own
absolute paths and never read `OPENCLAW_HOME`.

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
├── public-service-gate/invites.json (member store; invite codes retired)
├── public-service-gate/state.json
├── public-service-gate/ad-refs.json (promo ad ref code counts)
├── public-service-gate/handoffs.json (customers handed off to a person; bot silent)
├── cv-maker/ (CV orders; settled orders are purged)
└── cron-state/<promo>/state.json (per-promo delivery state)
```

Old `runtime/account-link/`, `runtime/job-scout/users/`,
`runtime/job-scout/dispatch-runs/` and `runtime/cron-state/` entries for the
retired crons are leftovers from removed services and can be deleted.

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
pin used for this comparison). The Railway image uses `node:26.12.1-bookworm-slim`; the pinned
OpenClaw supports Node 26, and its Node/SQLite and WhatsApp compatibility smoke
checks run before rollout. OpenClaw itself remains pinned to avoid a state
migration during the runtime comparison.

Promo campaigns can use one lazy gateway SDK client per process instead of
starting the full OpenClaw CLI for every group. Set
`GENAIE_CAMPAIGN_TRANSPORT=rpc` to opt in; `cli` remains the image default because
the SDK client retained about 165 MiB during idle send gaps in the initial trial.
The speedup has not established a net billing improvement. Sends retain account routing, spacing,
per-group state commits and stable per-day idempotency keys; an uncertain send is
never automatically retried through a second transport. Dry runs start no helper.

The `sender-trial` release profile selects `rpc-once`: the same upstream client
loaded through a version-checked narrow SDK re-export, with its helper joined
after each request so it holds no memory between sends. The `history-trial`
profile opts the existing history-owner cleanup path into five-minute idle
rotation; database writers and archive/reclamation workers keep upstream policy.
`allocator-trial` separately tests `MALLOC_ARENA_MAX=2`. Normal releases preserve
these settings and validate them at startup; exact-image rollback restores them.
`cache-trial` enables `GENAIE_WHATSAPP_CACHE_CLEANUP=1`: each WhatsApp metadata
owner releases already-expired local, reconnect and Baileys cache entries once
per minute. Valid entries, existing expiry times, mention data, reconnect
fallback and campaigns stay intact. The cleanup timer stops with its owner.
It is disabled by default; set the flag to `0` and redeploy to opt out.
See [expired metadata cleanup](deploy/railway/README.md#expired-whatsapp-metadata-cleanup)
for validation coverage and measurement limits.
The approved `standby-trial` profile enables daily overnight standby from
17:00 until 05:45 Africa/Algiers. Both WhatsApp accounts, replies and CV requests
are unavailable then. A container-local supervisor wakes the gateway without
this desktop, preserves stored state and enabled crons, and defers shutdown for
active work, pending deliveries or a campaign due before wake. This schedule is
separate from spending limits; no hard compute cutoff is installed. See the
[standby procedure](deploy/railway/README.md#approved-overnight-standby).
See [the memory trial procedure and nighttime measurements](deploy/railway/README.md#nighttime-measurements-october-28-2026)
before interpreting any short sample as sustained cost savings.

Use `python3 deploy/railway-memory-report.py` to inspect container, process and
recorded worker memory over SSH, or add `--memory` to the cost report. Fresh
worker counters (`--live-workers`) and the one-shot source-cache experiment
(`--release-source-cache`) require explicit opt-in; neither runs automatically.
See [memory attribution](deploy/railway/README.md#memory-attribution-november-2-2026)
for sample freshness, probe cleanup and the limits of billing comparisons.

The image also applies `deploy/railway/patch-worker-idle.py` to the pinned
model-catalog worker pool. It is **disabled by default**
(`GENAIE_CATALOG_IDLE_RETIREMENT=0`). A controlled trial with the flag set to `1`
enables upstream idle retirement after 60 seconds,
retaining the upstream five-minute anti-churn warm window on subsequent workers.
Active tasks, native cleanup and source-capture ownership stay with the upstream
pool. The baseline profile retains catalog workers indefinitely. Two actual
catalog/auth retire-and-recreate cycles and local model inference run in CI,
without production credentials or network access. Correctness alone does not
establish savings; promotion requires the full-week cost comparison. Version and source-anchor checks fail the build
if an OpenClaw upgrade needs a new compatibility review.

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

### Borehole provider catalog

`workspace/data/borehole-providers.json` is the private, versioned source of truth
for Same Age Projects (Bindura), Big Player (Harare) and Baltic Sea Drilling
(Harare). Edit each provider's offers separately; unknown prices are `null`.
Skylight Solar is excluded: its fixed $10/month campaign remains independent.
The public `borehole_info` action asks for customer location and requirements,
filters documented service coverage, and ranks nearest base first, then known
prices for equivalent offers. Combined drilling and installation requests are
matched to one supplier so prices are not mixed across companies. Town-centre distances come from the attributed
GeoNames extract in `workspace/data/zimbabwe-towns.json`; ambiguous locations
require clarification. No online geocoding runs during customer chats.
Only anonymized offers and conditions reach the customer agent. Provider
selection is retained for 24 hours, keyed by WhatsApp sender and receiving account,
in `runtime/public-service-gate/borehole-selections.json` alongside service state.
Borehole handoffs notify the operator with the internal provider selection;
provider names, base locations and direct contacts are never sent to customers.
Missing information requires a handoff; travel charges and final site quotes
are never assumed. Existing `PB`/`BP` tracking references remain recognized.

The two superseded registrations (borehole-paid and Big Player) were retired
when this consolidation shipped, and the retained `borehole-drilling-promo` job
kept its schedule and its runtime delivery history.

### Temporary CV promotion pause

The `cv` campaign is intentionally switched off: `enabled: false` on
`campaigns/cv`, which also leaves its `cv-promo` cron job disabled on the
gateway. Customer CV requests remain available during normal gateway hours, and
release checks only require the campaigns that are enabled. The original CV
poster still carries the offline ads contact number and must be corrected before
this campaign is turned back on. Switching it on is a console edit; the
reconciler enables the job.


### Console-created campaigns

The console API accepts authenticated `POST /internal/console/campaigns/create`
requests when its existing write key is installed, with
`GENAIE_AD_ASSETS_BASE_URL` configured. Campaigns, group assignments, image
index, audit and idempotency receipt save in one Firestore transaction. Every
campaign is data-defined now, so a campaign created here is the same kind of
thing as one converted from a repository script -- the only difference is the
job name it runs under.

The console signs direct, immutable R2 picture uploads using its own bucket-scoped
write token. The gateway needs no R2 write credentials. `console-campaign-create`
checks uploaded bytes before saving; `run-console-campaign` uses the shared
broadcast engine and the runtime read key. Caption overrides are also supported.

Desired schedules reconcile when the console API starts and every minute.
Stable `console-<slug>-promo` names and a volume lock recover uncertain gateway
acknowledgements without duplicate jobs. Read-back verifies the job; pending
campaigns cannot post or consume caps. Creation never triggers an immediate send.
Failure alerts are off unless explicitly configured. New scripts are included by
the existing workspace bin/lib staging rules.

Run `python3 workspace/lib/test_console_campaigns.py` and
`python3 deploy/test_console_campaign_api.py`, then the existing operator config,
writer, broadcast, read bridge and console API test suites before deploying.
Deploy bot support before enabling the console creation controls. Keep
`GCP_SERVICE_ACCOUNT_WRITE_B64` isolated to the console API as before.

### How the repository campaigns were converted (historical)

Ten campaigns kept their captions in `workspace/cron/<name>/post_ad.py`, their
roster in a generated `targets.json`, their pictures in an `assets/` directory
and their schedule in `deploy/promo-crons.json`.
`deploy/migrate-campaigns-to-firestore.py` moved all of that onto each
campaign's own Firestore document, one campaign at a time, so every one runs
through `run-console-campaign` exactly like a console-created campaign. All ten
were converted on 2026-11-11 and the files were deleted in the same change; the
conversion tool went with them, having nothing left to read.

What a conversion wrote, and what is therefore true of those campaigns now:
`kind: console`, `captions`, `tracking`, `schedule`, `timeoutSeconds`,
`failureAlertTo`, `images` and `jobName`, with an audit document in the same
commit.

`jobName` is the campaign's original cron job name, kept rather than renamed to
`console-<slug>-promo`. That is load-bearing: the run keeps using
`runtime/cron-state/<jobName>/state.json`, so sent-today state and
caption-rotation counters carried across the conversion instead of every group
getting the day's ad a second time.

Two rules the conversion enforced apply to every campaign the console creates
too (`console_campaigns.validate_definition` and `spacing_problem`): a schedule
must not be on the hour, and a campaign must not have more groups than its
timeout reaches at full spacing -- at most 30 groups per hour of timeout, 60 for
the 7200 second job. The live schedules are checked against the same arithmetic
on every release; see *Two things a campaign's schedule has to be able to
deliver*.

A run attaches the `images` on the campaign document, so
`deploy/upload-ad-image.py --write` refreshes that list whenever it changes a
campaign's index entries, and prints the update in its dry run.

## Campaign retirement from the console

The authenticated console API accepts `POST /internal/console/campaigns/<slug>/delete`
only when `GENAIE_CAMPAIGN_DELETION=1`, live Firestore configuration, the console
write key, and the existing R2 cleanup credentials are available. Acceptance
atomically commits a disabled retirement marker, audit snapshot and cleanup
receipt. The background worker retries owned cron removal, group assignment
cleanup, indexed/exclusive picture removal and cache cleanup after restart.
Readers and broadcast submissions respect retirement immediately; an already
submitted message can finish. Picture cleanup waits for bounded upload leases.

Converted campaigns retain their existing job name, and cleanup recognizes their
shared runner command. Conversion and reseeding refuse to restore retired state.
Installers check retirement before and after registration. Deploy this support
before enabling the console delete control. Deployment alone deletes nothing;
retain retirement-aware readers and receipts during rollback.

## Console schedule edits

With live Firestore configuration and the existing console write key, the bot
advertises `writes.scheduleEditing.edit`. Authenticated `POST /internal/console/schedule`
accepts a displayed date/timezone and changed `{slug, time, expectedRevision}` rows.
The writer atomically stores each change and its audit record. Built-in campaigns
receive a `scheduleOverride`; console-created campaigns keep their existing
schedule structure. Existing weekdays, timezone, commands and enabled states remain.

The response and campaign reads include `scheduleEdit` with an opaque revision,
editability, pending/ready status and last-confirmed displayed minute. The existing
console worker wakes after writes, verifies gateway jobs by reading them back,
and retries pending edits after restart. Cron installers, drift checks and the
borehole legacy-job migration respect saved overrides. Reseeding refuses to
silently replace console schedule edits. Changing a time never invokes `cron run`.

Local callers send the same request to `workspace/bin/console-schedule --apply`
on stdin; that mode attempts gateway reconciliation immediately. The console API
worker provides automatic retry for any pending updates. Repeating a request for
an already-saved time is safe; stale revisions reject changes as a whole.

## Independent schedule entries

A campaign's start time is one run a day. A campaign may instead carry
`scheduleEntries`: a map of independent recurring runs, each with its own cron,
timezone, `paused` switch and `skipDates` list. The bot advertises
`writes.scheduleEditing.entries` when `workspace/bin/run-schedule-entry` is
present, which is what makes the console draw its repeat, pause, skip and delete
controls; without it the console offers start times only.

A schedule request carrying an `operationId` is an entry request. Its changes are
`{slug, entryId, expectedRevision, create, time, paused, skipped, deleted}` rows,
applied atomically with their audit records. The `operationId` is a durable
receipt: replaying a request returns the stored result rather than writing again,
and reusing the ID for different content is refused. A campaign that has entries
refuses the older times-only path, so an out-of-date console cannot quietly
overwrite them.

Reconciliation registers one gateway job per entry. The entry named `primary`
keeps the job the campaign document already names, so adding entries never
restarts an existing run; every other entry gets `console-entry-<slug>-<entryId>`
running `run-schedule-entry`, which re-reads the campaign at fire time and exits
quietly when its entry is paused, skipped for that day, or deleted. Each entry
keeps its own send state, so two runs on one day do not deduplicate each other.
Deleting a campaign retires its entry jobs with it, and an entries campaign
counts as schedulable for caps only while one of its entries may still run today.

A job is created by `openclaw cron add` and then adjusted by `openclaw cron
edit`, and the two do not take the same options: `--enable`, `--disable` and the
whole `--failure-alert` family belong to `edit` alone, while `add` takes
`--disabled` in their place. openclaw fails the whole call on an option its
subcommand does not take, and a failed call is only counted as pending, so the
mistake shows up as a job that is never created rather than as an error anyone
reads. `console_campaigns.CRON_OPTIONS` is the list of options each subcommand
is passed; `gateway` refuses anything outside it before openclaw is invoked, the
fake gateway in the tests refuses the same, and a test checks the list against
`openclaw cron <subcommand> --help` wherever openclaw is installed. Add a new
flag to that list in the same change that starts passing it. When a
reconciliation does stay pending, the reason now travels out through the
bridge's JSON as `scheduleProblems` and the console API logs it once per change,
because the bridge never leaks stderr.

## Indexed live console views

The console's Schedule home page, paginated Groups/Campaigns views, and assignment
pickers use `/internal/console/live/*`. A single demand-driven Firestore Listen
stream maintains `console-live.sqlite` in the private Firestore state directory.
The stream covers settings, lines, campaigns, groups and campaign-deletion receipts;
it never watches audit entries or asset bytes. No display query downloads config
collections. The initial sync, a credential/schema change, or an existence mismatch
can require a complete bootstrap. Restart and idle reconnects resume from durable
positions. Bootstrap and reset generations publish only at consistent boundaries.

Visible console resources poll every 30 seconds and renew a 90-second activity
lease. When the lease expires, the stream disconnects and the index stays on disk.
Group DMC/member requests read only local telemetry for requested IDs. Discovery
requests read local inventories and indexed membership without invoking config
loaders. Metrics keep their own observation timestamps. Stale config remains
viewable with synchronization status; saves still validate live Firestore state.
Successful writes fence displays until the stream passes the write boundary.
After a write, the API resumes the shared stream from its durable positions to
obtain a prompt consistent boundary instead of waiting for an idle heartbeat.
The bounded wait leaves local telemetry available. If confirmation is delayed,
revision-aware polls return stale/unchanged metadata and retain displayed data;
uncertain writes are never replayed to make the display catch up.

The API validates and authenticates the live routes independently of the legacy
read bridge. `groups`, `campaigns`, `discoveries` and `preview` return at most ten
rows plus query/revision-bound continuation cursors. `q` retains case-insensitive
substring matching, and filters apply before pagination. Changed revisions return
an anchor-based current window; unchanged polls return only freshness metadata.
`metrics` accepts up to 100 validated group IDs. `schedule` contains only timeline
slots and editing capabilities. Campaign metadata, captions/pictures and previews
have separate requests. `/live/status` exposes sync lag and counters for indexed
requests, returned rows, streamed documents, rebuilds and active streams.

The runtime image and portable CI install `deploy/railway/requirements-console.txt`.
For local testing, install it in a virtual environment before running the index
protocol tests. Existing CLI config readers, campaign runners and all writer
validation remain authoritative and independent of this display index. Deploy the
bot endpoints before the updated console; it intentionally has no full-directory
fallback when a live endpoint is missing. Roll back the console before the bot.

## Backups of the operator data

Git used to be the backup and the history for this data: the groups, the
campaigns, their copy and their pictures sat in the repository, so any mistake
was one `git revert` away. Once they live only in Firestore and R2 that is gone,
and the last-known-good snapshot in `runtime/firestore/` is not a substitute --
it holds one generation, it is overwritten by the next healthy read, and it
expires after 48 hours. `deploy/backup-operator-data.py` is what replaces git.

```bash
deploy/backup-operator-data.py --key seed-key.json                  # write today's backup
deploy/backup-operator-data.py --key seed-key.json --verify         # newest backup vs. live
deploy/backup-operator-data.py --key seed-key.json \
    --restore runtime/backups/2026-11-11 --collection groups        # what it would put back
```

A backup is a directory named for the day under `runtime/backups/` on the
volume, holding one JSON file per collection plus `manifest.json` with each
file's document count and sha256. Documents are decoded exactly as a campaign
run decodes them, so a backup is readable by a person and can be written
straight back. The directory is assembled under a temporary name and renamed
into place, so a half-written backup is never mistaken for a whole one, and
every file is `0600` inside a `0700` directory.

Nine collections are copied: the four config collections, the `assets` picture
index, the `audit` trail -- which is the real change history, with the before and
after of every console write -- and the three that record that an operation
already happened (`campaign_creations`, `campaign_deletions`,
`campaign_asset_uploads`), because restoring the config without them would let a
replayed request run a second time. The console's live index is deliberately not
copied: it is a derived SQLite cache, rebuilt from these.

R2 holds the picture bytes, which are far too large to copy and are already
content-addressed, so what is recorded is the object list: every key, its size
and R2's own etag. Listing the bucket needs the S3 credentials the entrypoint
deliberately strips from the gateway environment, so the weekly cron records the
Firestore side and says in the manifest that the bucket was not listed; a run on
the operator's machine, which has `~/.config/genaie/r2-upload.env`, records both.
`--no-r2` skips the listing even where it is configured.

`--retain N` keeps the newest N backups and deletes the rest (default 8, `0`
keeps everything). Anything that is not a dated directory with a manifest is
left alone, so an operator's own copy beside them is never pruned.

`--restore` writes whole documents, not the field patches `console-write` is
limited to, because a restore has to be able to put back a field no console
screen can edit. That is also why it is one collection per run, dry by default,
and prints every document it would change; `--write` applies it and commits one
audit document of its own. A document created since the backup is listed and
left alone -- deleting it is a separate, deliberate act through the console.

A backup needs only read access, so the gateway's read-only runtime key is
enough; a restore needs Cloud Datastore User, so it needs the seed key. Register
the weekly run by hand, as every other cron on this gateway is registered --
merging this repository schedules nothing:

```bash
railway ssh -- env -u OPENCLAW_HOME HOME=/home/joseph \
  OPENCLAW_GATEWAY_TOKEN="$GATEWAY_AUTH_TOKEN" openclaw cron add \
  --name operator-data-backup --schedule "40 3 * * 0" --tz Africa/Harare \
  --command "python3 /home/joseph/.openclaw/deploy/backup-operator-data.py"
```

Pull a backup to your own machine with `railway ssh -- tar cz -C
/home/joseph/.openclaw/runtime/backups .` and keep at least one copy off the
volume: a backup that only exists beside the thing it is backing up is not one.
