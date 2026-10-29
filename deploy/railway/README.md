# OpenClaw gateway — Railway deploy

The OpenClaw gateway (agent + internal cron + WhatsApp session) runs on **Railway**
(project `openclaw-gateway` / service `gateway` / environment `production`, EU West).
This directory holds the build that Railway ships. **This repo is the single source of
truth** — there is no hand-maintained `live-source/` copy anymore.

## How a deploy happens (the pipeline)

```text
PR → portable validation + secret scan + installed runtime compatibility
merge to main → repeat all checks → serialized release job → startup verification
                                                       ↘ exact image rollback on failure
```

`.github/workflows/deploy-main.yml` has three prerequisite jobs:

- **Portable validation** runs structure, documentation contracts, syntax, Python
  and Node tests, ShellCheck and actionlint on Node 26.12.1.
- **Secret and private-state scan** checks the source with gitleaks.
- **Installed runtime compatibility** builds the production Dockerfile with
  Node 26.12.1 and OpenClaw 2026.9.9, then tests SQLite, WhatsApp LID recovery,
  the installed gateway SDK, generic pool safety, and actual catalog/auth worker
  retirement and recreation. The container has no external network or production
  state. Required integration tests cannot skip. This job takes about ten minutes.

`Deploy verified production commit` depends on all three jobs and runs only for
`main`. It uses a production project token, uploads a temporary `git archive` of
that exact tested SHA, and records deployment ID, image digest and runtime profile
in the `railway-release` artifact. Local dirty and untracked files cannot enter
the upload. Railway rebuilds the same source; the recorded digest identifies the
image it actually produced, rather than asserting that separate builds are identical.

The job checks a bounded, sanitized startup log record for the baked source SHA,
campaign transport, catalog/history policy, allocator setting, both connected WhatsApp accounts and all twelve
campaigns. Failure restores the previous successful deployment's
exact image digest and owned runtime settings, then verifies its startup. It
never installs crons or sends a test message. Startup verification for both release
and rollback allows ten minutes, covering the entrypoint's 330-second stale-lease
handoff plus channel startup. The release job has a sixty-minute ceiling to leave
time for recovery. This needs no SSH key, public
health endpoint or permanent monitoring process. Deployments are serialized;
queued releases whose SHA is no longer `main` exit without changing production.

**The gate only controls production after the one-time cutover below.** Until
`RAILWAY_TOKEN` is provisioned and Railway's direct GitHub source disconnected,
the existing integration can still deploy `main` before checks finish. Branch
protection is unavailable on the current repository plan; do not treat it as an
active safeguard.

The Docker build stages the curated overlay at `/opt/live-source`; the entrypoint
syncs it onto the persistent volume at boot. The source marker records the tested
SHA, and the release verifies that marker instead of trusting a mutable checkout.

To change what the gateway ships (workspace code, cron jobs like the CV-promo ad targets,
agent context markdown, the public WhatsApp system prompt, the business info page):
edit it in this repo, open a PR, merge. That's the whole flow. Three sets of
`openclaw.json` settings are synced on every boot by scripts baked at `/usr/local/bin/`:

- `deploy/railway/sync-public-prompt.py` copies
  `workspace/docs/public-whatsapp-system-prompt.txt` into
  `channels.whatsapp.direct["*"].systemPrompt`.
- `deploy/railway/sync-reply-gates.py` sets the eight WhatsApp reply gates
  (`channels.whatsapp.enabled`, `dmPolicy`, `accounts.default.enabled`,
  `plugins.entries.whatsapp.enabled`, both `public-service-gate` `enabled` flags
  and the two `whatsapp-relevance-gate` ones) plus the CV payment details
  (`public-service-gate` `cvMakerPaymentDetails`) to their
  `config/openclaw.template.json` values, and appends `"*"` to
  `channels.whatsapp.allowFrom` when `dmPolicy` is `"open"` — without it every DM
  is dropped. It writes scalar leaves only and never touches `bindings`,
  `groupPolicy`, the `ads` account or any `PRESERVE_PATHS` entry. Set the Railway
  variable `REPLIES_DISABLED=1` to turn the `default` line's replies off instead of
  editing the volume; it does not touch the `ads` account, which has no tracked
  off-switch here (editing the template does not reach the live config on Railway —
  silence the `ads` line by hand-editing the live `openclaw.json`, an edit the sync
  does not revert).

`deploy/railway/sync-runtime-plugins.py` also applies eleven optional plugin
`enabled` leaves from the template. It disables unused browser, canvas,
computer/device, geolocation, GitHub, Ollama, voice, Anthropic and xAI integrations.
It preserves plugin configs, model access, memory, file transfer, local execution,
WhatsApp gates, bindings and both accounts. To restore a governed plugin, set its
template `enabled` boolean to `true` and deploy. `--dry-run` reports without writing;
invalid input leaves the config untouched and does not block boot.

The rest of `config/openclaw.template.json` is still not applied (see README).
`deploy/railway/wait-gateway-lease.py` (also baked at `/usr/local/bin/`) runs just
before the gateway and waits up to 330 s for a stale gateway owner lease left by a
killed container to expire, so a deploy that lands mid-cron-run does not crash-loop
(look for `[entrypoint] gateway owner lease held by another host` in the boot log).

Cron job definitions (schedule, command, job timeout) are the exception to "merge and
it ships": they live in the gateway's cron state on the volume and change only when
their `deploy/install-*.sh` installer is re-run (via `deploy/gateway-exec.sh`, i.e.
`railway ssh`). For example, the promo crons' 7200 s job timeout set in
`deploy/install-promo.sh` reaches production only after the twelve
`install-*-promo.sh` installers run again; each installer now verifies the
timeout it registered is the one the gateway reports back, and the promo engine
reads that live value at run time so a not-yet-migrated job tightens its send
gaps rather than being killed with groups unreached (see README).

The same applies to the promo **schedules**, which moved off the hour (07:15,
08:13, 08:40, 10:35, 11:39, 12:50, 13:21, 14:26, 17:06 `Africa/Harare`) so the
account's daily pattern is not perfectly periodic. (big-player runs at 15:50,
industrial-lighting at 10:05, and genaie-advertisers at 14:05 primarily on the
"ads" line. All three also serve BULAWAYO ADVERTISEMENT on the default line,
so these slots clear both accounts' preceding campaigns.) Merging
that change ships nothing on its own. `deploy/promo-crons.json` is the declared
cron state for all twelve campaigns --
schedule, timezone, failure-alert target, command and the shared 7200 s job
timeout -- and `deploy/reinstall-promo-crons.sh` is the one command that compares
it to the gateway and applies it:

```sh
./deploy/reinstall-promo-crons.sh --check   # read-only: live state vs. what the repo declares
./deploy/reinstall-promo-crons.sh           # re-run all twelve installers, then re-check
```

`--check` makes a single `openclaw cron list --all --json` read and changes
nothing. It exits 0 when the gateway matches the manifest, 1 on drift, 2 when the
manifest or the live payload is unreadable, and 75 when the gateway is
unreachable and nothing was inspected. Drift is reported per field, along with
any job the manifest does not declare.

**Run `--check` first and delete anything it reports as undeclared before
reinstalling, or the ad goes out twice.** `register_promo_cron` matches on the
job *name* only, so a leftover job under a different name — a rename, a
hand-created job, an abandoned experiment — keeps firing the same ad on its old
schedule and the installers would never notice it.

`--only <promo>` retries a single campaign, `--dry-run` prints the plan without
contacting the gateway, and `--json` is for an automated caller.

The installers are unchanged and still do all the writing: the orchestrator execs
`deploy/install-<promo>-promo.sh` one at a time in schedule order, continues past
a failure, and exits 1 with a summary naming what failed -- so one stale promo
cannot leave the other ten unmigrated. `register_promo_cron` reads the schedule,
timezone, enabled flag and timeout back and fails loudly (printing the job's
current state) when any of them is not what it just set: nothing else would catch
a stale schedule, which is precisely what these installers exist to change.

Reconcile **outside the campaigns' window** -- before 07:15 or after 17:06
`Africa/Harare` (05:15 / 15:06 UTC; Harare has no DST) -- so no promo's schedule
moves while that promo is mid-run. A same-day second firing is safe in itself:
each delivered group is committed immediately, so the re-run skips the groups it
already reached.

Group membership and the per-group ad caps are NOT cron state: they live in
`workspace/cron/membership.json`, which ships with the workspace overlay on a
normal merge. Turning the caps on is a one-line edit to that file
(`defaults.caps_enabled`), not an installer run.

## One-time wiring

1. Create a **project token** for `openclaw-gateway`, environment `production`,
   and save it as the repository Actions secret `RAILWAY_TOKEN`. Keep the token
   out of chat, Git and logs. This is the only release credential; no workspace
   billing token or SSH key is needed. Create it through the project owner's
   Railway settings access.
2. Confirm the correction PR passes all three prerequisite jobs and the secret
   exists. Keep the existing volume mounted at `/home/joseph/.openclaw`.
3. Disconnect the service's direct GitHub source **before merging the correction
   PR**, so merging triggers only the checked Actions release:

   ```sh
   railway service source disconnect \
     --project a251f752-3b49-4f72-8a71-4629bf2fced0 \
     --service 503baf60-c897-4a77-812a-719195705031 \
     --environment 2cae37a4-8e24-41db-87ed-9a3a7c9eff22 --json
   ```

4. Complete the live cron preflight below, then merge, watch the `main` workflow,
   and inspect its `railway-release` artifact.
   Confirm `healthy`, the tested SHA, image digest, selected runtime flags, both accounts,
   all jobs, and no `rollbackError`. A missing secret fails the job before any
   production mutation; provision it before the cutover to avoid a deployment gap.

Require the three status checks and PRs in branch protection if the repository
plan later supports it. The production gate is the deploy job's `needs` list
combined with the disconnected direct source.

## Cron preflight before merging

Check production cron activity immediately before a planned merge. The main
workflow spends about ten minutes in compatibility checks before deployment
starts, so reserve time for CI, the build, startup verification and a possible
rollback. Prefer a quiet gap with at least thirty minutes until the next
campaign, and check again if the release is delayed. Keep the PR open while
campaigns are running or the available gap is too short.

Read the live schedule using the existing local operator login and SSH key:

```sh
export RAILWAY_PROJECT=a251f752-3b49-4f72-8a71-4629bf2fced0
export RAILWAY_SERVICE=503baf60-c897-4a77-812a-719195705031
export RAILWAY_ENVIRONMENT=2cae37a4-8e24-41db-87ed-9a3a7c9eff22
source ./deploy/gateway-exec.sh
"${GATEWAY_OPENCLAW[@]}" cron list --all --json
```

Require empty `state.runningAtMs` markers, no active campaign processes under
`/proc`, and no unfinished `cron_run_receipts` in the live state database. Use
the live `nextRunAtMs` values to find the next campaign; installers can leave
live schedules different from the repository manifest. Leave cron definitions
enabled and let active work finish normally.

The startup report runs after the gateway restarts, so this operator preflight
must happen before merging. GitHub CI's release gate validates the tested
source and startup; it does not acquire a pre-deployment cron lock.

## Recovery

The release refuses to mutate production without a known previous image digest.
On failure it cancels a still-pending candidate by deployment ID before restoring
the exact previous successful image and verifying startup. A missing new image
digest, build, provenance, runtime flag or channel/job failure triggers recovery. Read the
release artifact for its rollback deployment ID and digest. If rollback fails,
`rollbackError` is recorded and the workflow remains failed; a
`candidateCancellationError` also requires inspection for a late build using the
unique `releaseMessage` from the artifact. An ambiguous upload without a confirmed
deployment ID is never treated as fully recovered. In either case, inspect that specific
deployment and restore a known healthy image through Railway before retrying.
Do not downgrade migrated OpenClaw state.

To deploy a corrected release normally, open a PR and use the same pipeline. To
repeat a verified release, manually run the workflow on `main` with profile
`preserve` (the default). Normal pushes also preserve the existing catalog
retirement setting: `1` resolves to `catalog-trial`, `0` or an absent variable
resolves to `baseline`. An unrecognized value aborts before mutation. Explicitly
choose `baseline` to disable retirement or `catalog-trial` to enable it. Artifacts
record `requestedProfile` and the resolved `profile`, and startup verifies the
resolved setting. Failed releases still restore the prior image and settings. Avoid `railway up` from a working checkout: it can include unrelated
local files, bypass the CI gate and obscure which source actually ran.

> The old `/home/joseph/openclaw-deploy/` directory (with its own `live-source/`,
> `railway/Dockerfile`, `entrypoint.sh`) is **deprecated** — kept only as historical
> seed/volume tooling. Do not edit `live-source/` there; it no longer feeds production.

## Volume seeding & recovery

The gateway's final boot command preloads the repository's
`whatsapp-lid-loader.mjs` compatibility patch. It preserves incoming DMs when
WhatsApp supplies an opaque sender ID without a phone mapping, and archives
account-scoped routing hints under `runtime/whatsapp-identities/` so relinking
cannot erase them. Cached phone aliases never establish restricted access or
administrator authority without current evidence. It also preserves LIDs in
outbound targets and command-owner comparisons. Standalone CV delivery adds the
loader to its CLI environment, so manual approval does not depend on inheriting
the gateway's environment. The loader covers plugin captures as well as the installed adapter and
checks its patch anchors against the pinned OpenClaw version. See the root
README's WhatsApp reception troubleshooting before upgrading OpenClaw; a healthy
channel status alone does not verify inbound messages and replies.

Durable state is a Railway volume at `/home/joseph/.openclaw` (seeded once from the local
Docker volumes). Seed mode (`OPENCLAW_SEED_MODE=1`) idles the container so state can be
pushed via the CLI; auto-seed extracts `openclaw-state.tgz` / `runtime-seed.tgz` from the
volume root on boot. See the entrypoint (`deploy/railway/entrypoint.sh`, steps 0–1.6) and
the project memory notes for the full seed/stuck-session recovery procedure.

## Verify a deploy

```sh
railway logs        # expect: [whatsapp] ... starting provider, [gateway] ready,
                    #         [entrypoint] synced source overlay from /opt/live-source/workspace,
                    #         [entrypoint] public WhatsApp prompt updated|unchanged
                    #         [entrypoint] reply gates unchanged (9 paths already match the template)
```

The release artifact also retains the `GENAIE_RELEASE_READY` record emitted once
at startup; it contains no credentials, phone numbers or raw account bindings.

A `reply gates updated: ...` line instead means the live config had drifted and was
corrected; `reply gates NOT synced: ...` means it was left alone and the previous
values still apply. A `WARNING: openclaw config validate failed after the
reply-gate sync` line is never fatal but always worth reading.

## Cost measurement

The October 11 OpenClaw/Node upgrade coincided with a sustained memory increase.
Memory represented about 91% of compute usage on October 27. The target is about
$10/month, retaining both WhatsApp lines and core bot capabilities. Keep one
replica and the gateway always running; sleep would interrupt WhatsApp and cron.

The workspace compute email alert is $8, with no hard shutdown limit. Inspect it
with `railway usage limit status --json`. A forecast is not a spending cap.

Railway's historical resource charts and actual invoice line items are the
source of truth. Reports below are **manual snapshots** using the local login;
there is no scheduled collector, extra billing credential or added hosting cost.
The report keeps provider forecasts, linear resource projections and invoices
separate. On October 27 the provider forecast was about $12.20, while
period-to-date spend extrapolated over the actual 31-day cycle was about $22.46.
Neither number proves that the target has been reached.

For each stable profile, capture an initial snapshot, then another after seven
full days, outside the repo:

```sh
python3 deploy/railway-cost-report.py --since 1h > /tmp/railway-baseline-start.json
# Seven full days later, with the same baseline image/profile:
python3 deploy/railway-cost-report.py --since 7d \
  --baseline /tmp/railway-baseline-start.json > /tmp/railway-baseline-week.json
```

The comparison reports resource spend deltas per day only within the same scope
and billing cycle. Across a cycle boundary use Railway history instead. The
metric window can be frozen with `--since <UTC timestamp> --until <UTC timestamp>`.
Current flags are not proof of past configuration; use the deployment timeline
and release artifacts to exclude intervals containing an unintended rollout.

Establish the Node 26 baseline with `GENAIE_CAMPAIGN_TRANSPORT=cli` and
`GENAIE_CATALOG_IDLE_RETIREMENT=0`. Then manually run the checked workflow on
`main` with profile `catalog-trial` for a matched seven-day candidate window.
Keep Node, OpenClaw, plugins and sender unchanged. Compare **total resource cost
per day** and campaign volume, not just idle RSS. Promote retirement only if the
candidate reduces that rate by at least **5%**, with no missed/duplicate sends,
worker failures, unexpected restarts, channel/CV failures or latency regression.
If inconclusive or worse, explicitly run the baseline profile again. Normal
pushes preserve the selected setting, so unrelated deployments do not end a trial.
A source deployment still changes the experiment: record its timing and compare
matched workloads before declaring savings.

Check both connected WhatsApp accounts, inbound replies, CV processing and the
next normal campaign runs; do not send extra promotional test messages. Retain
the $8 email alert and no hard shutdown cap. Do not lower the memory ceiling or
downgrade migrated state to force a billing target. The $10 goal remains unproven.

An allocator trial is separate from plugin trimming: set `MALLOC_ARENA_MAX=2`
through Railway variables, restart, and compare equivalent settled windows against
the plugin-only deployment. Keep it only if memory improves without worker errors,
restarts or functional regressions; otherwise remove the variable and restart.
It is deliberately not baked into the image without measurements. If the target
is missed, profile worker heaps and native memory next; lowering only the main
JavaScript heap limit does not bound the workers or native allocations.

### Initial rollout: 2026-10-27

The plugin policy was deployed successfully: startup now reports 8 loaded plugins
instead of 19. Both WhatsApp accounts connected without channel errors, and all
12 campaigns and both feeds remained enabled. CV and feed CLI smoke checks and
the portable test suite passed.

Twelve idle samples per deployment, ten seconds apart, gave these averages over
the last six samples (MiB):

| Configuration | Container memory | Anonymous memory | Gateway RSS |
| --- | ---: | ---: | ---: |
| Optional plugins disabled | 1658 | 1062 | 1073 |
| Same image, `MALLOC_ARENA_MAX=2` | 1593 | 1014 | 1020 |

The short allocator trial is inconclusive: its small difference includes cache
variation and neither run covers campaign load. The variable was removed rather
than making the experiment permanent. The $10 target is **not yet verified**;
measure seven full days from the verified baseline release before declaring savings.

The earlier memory-pressure sample found a 323 MiB main heap plus 520 MiB of
sampled worker heaps, with incomplete worker coverage. The largest sampled worker
was the prepared model catalog (130 MiB), followed by session transcript storage
(72 MiB), state reads (46 MiB), SQLite storage (50 MiB) and disk budgeting (42 MiB).
This points the next profiling pass at core worker lifetime/retention and native
RSS, rather than imposing a smaller main JavaScript heap or reverting migrated
state. Loaded plugin count alone does not establish proportional memory savings.

### Runtime and campaign optimizations

The runtime is pinned to Node 26.12.1 while OpenClaw stays at 2026.9.9. Compare the
runtime-only deployment against Node 24 before attributing savings to the changes
below; the full-week billing comparison remains the acceptance test.

Setting `GENAIE_CAMPAIGN_TRANSPORT=rpc` enables one lazy Node gateway SDK
client per promo campaign. The image keeps `cli` as its default because the SDK
client retained about 165 MiB during idle send gaps in the initial measurement;
faster startup alone does not establish a net cost reduction.
Python talks to that helper over private stdin/stdout;
no extra service or network listener is created. The helper connects to the local
gateway using its existing authentication, sends images through the normal
gateway media path, and closes when the campaign exits. Existing account routing,
random send gaps, queued-reconnect handling and per-group commits remain in the
broadcast engine. Stable per-day/content/account idempotency keys accompany sends.
The helper never replays a request or falls back to the CLI after a timeout or a
lost acknowledgement. Dry runs never start it. Set
`GENAIE_CAMPAIGN_TRANSPORT=cli` and redeploy to restore per-message CLI startup.

`patch-worker-idle.py` makes one guarded change to the installed OpenClaw 2026.9.9
model-catalog pool: its workers become eligible for upstream idle retirement after
60 seconds. The upstream five-minute anti-churn window on subsequent workers,
active-task protection, native-section draining and source-capture cleanup stay
intact. Catalog requests already carry the complete reconstruction input. Database
worker lifetimes are unchanged; the canonical-validation pool already closes at
the end of each batch. A changed OpenClaw version or patch anchor fails the build.
The image and explicit baseline release set `GENAIE_CATALOG_IDLE_RETIREMENT=0`.
An explicit `catalog-trial` release enables it, and subsequent ordinary releases
preserve that choice. Correctness passes do not establish
a cost benefit.

The additional workflow profiles change one setting while preserving the others:

| Profile | Setting enabled | Restore that experiment |
| --- | --- | --- |
| `history-trial` | `GENAIE_HISTORY_IDLE_RETIREMENT=1` | Set it to `0` and redeploy |
| `allocator-trial` | `MALLOC_ARENA_MAX=2` | Set it to `0` (glibc default) and redeploy |
| `sender-trial` | `GENAIE_CAMPAIGN_TRANSPORT=rpc-once` | Set transport to `cli` and redeploy |

`preserve` retains all four runtime settings, including catalog retirement.
`baseline` restores CLI transport, stock catalog/history retention and glibc's
default arena policy; `catalog-trial` selects that baseline plus catalog retirement.
The release records all settings and validates them in the startup report;
rollback restores the previous image and all owned settings. These trials must
be measured separately before combining their results.

`patch-history-idle.py` shortens only the existing history, projection and
maintenance owner's idle rotation from thirty to five minutes when opted in.
The owner still waits for active scopes to release, rotates/joins native workers,
and only then releases database custody. Pool auto-retirement remains disabled.
Writer, lease-heartbeat, archive/reclamation and usage-worker policies stay
upstream. The required runtime test uses real SQLite databases and all three
history lanes, verifies two retirement/recreation cycles, active-scope protection,
integrity/data preservation and opt-out behavior. Do not extend this patch to
other database owners without their own lifecycle tests.

The `rpc-once` campaign transport loads the identical upstream `GatewayClient`
through the pinned SDK's narrow client re-export. It starts one helper per
request and joins it before returning, so no SDK process remains during random
send gaps. It preserves account/media parameters, deterministic idempotency keys,
per-group commits, reconnect queuing and the no-replay rule after an uncertain
acknowledgement. The loader rejects a changed OpenClaw version or export contract.
The persistent `rpc` transport remains available for controlled comparisons.

An isolated October 29 fixture benchmark issued three read-only status requests
per transport. Mean peak process RSS was about 215 MiB for the stock CLI and
113 MiB for the narrow one-shot sender; mean process CPU was 2.58 versus 0.86
seconds. These are local startup measurements, not production send latency or
monthly savings. Two paired allocator tests each created/retired three real
history lanes through three cycles. Settled RSS was 470/417 MiB with default
arenas and 482/413 MiB with two arenas: no repeatable settled-memory reduction.
Keep the allocator trial disabled unless a matched production comparison supplies
better evidence.

### Nighttime measurements: October 28, 2026

Railway's four-minute samples from 20:00–23:00 UTC (21:00–00:00 Algeria;
22:00–01:00 Zimbabwe) averaged 1.343 GB, ranging 1.316–1.404 GB, at 0.0246 CPU
cores. The earlier 06:00–14:00 UTC window averaged 1.784 GB and peaked at 3.016 GB.
These are decimal GB from Railway, not GiB. The deployment at 14:12 UTC changed
the release between these windows, so the 25% difference is descriptive, not a
controlled estimate of cron overhead. The last promo completed around 15:43 UTC;
the live nighttime status also reported zero active cron runs.

SSH snapshots around 23:35–23:53 UTC found roughly 1.09–1.10 GiB gateway RSS,
with a 286–304 MiB main heap in use and two helper processes totaling about
101 MiB RSS (some shared pages). The initial cgroup reading was 1.31 GiB,
including about 127 MiB file cache and 66 MiB kernel memory. Source inspection
subsequently increased file cache; exclude that disturbance from an application
memory comparison. The latest detailed worker sample was from 15:37 UTC, with
456 MiB in direct worker heaps and partial coverage. It describes daytime work
and cannot be combined with nighttime counters as a precise RSS partition.

### Installed runtime verification

The required compatibility job exercises the actual catalog owner and worker,
loads provider models and authentication, performs real model inference against
a local stub, waits for two retire/recreate cycles, checks source-capture cleanup,
and repeats authentication and inference after each recreation. It also tests
an authentication failure and verifies that opt-out retains the worker. Fake
credentials and a network-isolated container keep this separate from production.
A generic echo worker or a cached `models.list` response alone is insufficient.

To reproduce the installed-runtime checks locally, build the image then run:

```sh
docker build -f deploy/railway/Dockerfile -t genaebot:compatibility .
docker run --rm --network none --entrypoint /bin/bash \
  -v "$PWD:/repo:ro" -w /repo \
  -e GENAIE_REQUIRE_INTEGRATION=1 \
  -e GENAIE_OPENCLAW_ROOT=/usr/local/lib/node_modules/openclaw \
  genaebot:compatibility -lc 'node --test deploy/railway/*.integration.test.mjs'
```

The sender tests cover malformed/trailing/partial responses, wrong IDs, EOF,
oversized replies, bounded blocked writes, helper restart, initial connection
failure, later recovery and no replay after a lost acknowledgement. A rejected
connection affects that request; a later distinct request can use a recovered
SDK connection. Cleanup cannot replace the original delivery error.

The initial Node 26 read-only comparison took 1.245/1.175/1.167 seconds for three
separate CLI channel-status requests versus 0.475/0.007/0.008 seconds using one
SDK client. These numbers measure client startup and status requests, not WhatsApp
send latency or monthly savings. The runtime-only idle memory samples did not
establish a Node 26 memory improvement; retain the full-week acceptance check.

An earlier manual trial confirmed Node 26.12.1, the transient `cli` default, both
WhatsApp accounts connected, and all 12 campaigns and both feeds enabled. The
catalog pool went from one idle worker to zero; model listing still returned all
793 entries afterward. Container memory went from 1537 to 1406 MiB across that
idle interval. These short samples also include normal GC/cache variation and
do not establish monthly savings or catalog reconstruction correctness. Subsequent
`main` deployments restored Node 24 and the stock worker policy. The corrected
release returns to pinned Node 26 with retirement disabled; use its deployment
artifact to identify the start of the baseline. No extra promotional test
messages were sent.

## Turning replies off

```sh
railway variables --set REPLIES_DISABLED=1 && railway redeploy   # replies off
railway variables --unset REPLIES_DISABLED && railway redeploy   # replies back on
```

Both states are enforced on every boot, so this is the supported way to stop the
bot replying. Do not hand-edit `openclaw.json` on the volume: the next deploy
restores the tracked values, and the edit leaves no trace in git (that is how the
2026-10-18 reply outage went unnoticed). To diff the live config against the
template without changing anything:

```sh
railway ssh -- python3 /usr/local/bin/sync-reply-gates.py \
  /opt/live-source/config/openclaw.template.json \
  /home/joseph/.openclaw/openclaw.json --dry-run
```

Note that the `openclaw` CLI invoked through `railway ssh` needs
`env -u OPENCLAW_HOME`, or it looks for the config at
`.openclaw/.openclaw/openclaw.json` and fails; see "Operational Changes" in the
repository README.

Do **not** trigger a manual ad send to test — the CV-promo campaign delivers on its own
cron at 11:39 Africa/Harare (declared in `deploy/promo-crons.json`; confirm the live
schedule with `./deploy/reinstall-promo-crons.sh --check`).

## Retired daily job feeds: 2026-10-28

Vacancy Lamp and Hustlers Marketplace job posting and the shared scraping/cache
pipeline are removed. The twelve promo campaigns and both WhatsApp accounts
remain required by release verification. Historical rollout notes above describe
the earlier feed-enabled deployments.

Before the removal deploy, save the feed definitions outside Git, verify neither
feed is running, and run `deploy/retire-job-scout-crons.sh --dry-run`, then run it
without the flag. This retires current and legacy feed names via the gateway CLI;
source deployment alone does not remove volume-backed cron registrations.
Deploy through the checked release workflow after the live cron preflight.
The boot overlay uses `rsync --delete` to remove the retired source and wrapper.
Verify the feed names are absent, both accounts connect and all twelve campaigns
remain enabled. Do not send test adverts. Archive only
`runtime/job-scout/group-feed/` under a dated `retired/job-feeds/` directory on the
volume; retain cron receipts and all other runtime state. Restore the archived
cache, previous source image and saved cron definitions only if undoing retirement.
Memory savings require subsequent measurements and are not a release criterion.
