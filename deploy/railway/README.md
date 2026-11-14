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

The job first installs Railway CLI 5.71.0. Its npm postinstall downloads a GitHub
release binary, so npm registry retry settings alone cannot recover a 504 from
that download. The workflow retries installation and `railway --version` up to
three times, waiting 10 then 20 seconds, within a five-minute step timeout. An
exhausted installation failure stops before the release helper runs and does not
upload an image. To recover a transient installation failure, open the failed
GitHub Actions run and choose **Re-run failed jobs**. This retries the deploy job
using the already-tested commit; the release helper still checks that it is the
current `main` before changing production.

The job checks a bounded, sanitized startup log record for the baked source SHA,
campaign transport, catalog/history policy, allocator setting, the connected main WhatsApp account and all ten
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

Cron job definitions (schedule, command, job timeout) are the exception to "merge
and it ships": they live in the gateway's cron state on the volume. For the ad
campaigns they are no longer in this repository at all. Each campaign is one
`campaigns/<slug>` Firestore document carrying its schedule, timezone, run
timeout, failure-alert number and switch, and the console API's reconciler
brings the gateway's cron into line with it -- normally within five minutes of
the edit, and again on every boot. Nothing here has to be re-run after a merge.

What the gateway actually has, and what the console says it should have:

```sh
./deploy/gateway-campaigns.sh | python3 -m json.tool   # every campaign, as the console sees it
railway ssh -- openclaw cron list --all --json         # the jobs themselves
```

A campaign converted from a repository script keeps its original `<slug>-promo`
job name (recorded as `jobName`); one created in the console gets
`console-<slug>-promo`. Either way the command is
`python3 <workspace>/bin/run-console-campaign <slug>`, and the job name is also
the `runtime/cron-state/<jobName>/` directory holding today's sends and the
caption rotation -- which is why the converted names were kept rather than
tidied up.

There used to be ten `deploy/install-*-promo.sh` installers, a
`deploy/promo-crons.json` declaring what the ten jobs should be, and
`deploy/reinstall-promo-crons.sh` to compare and apply it. All of that is gone:
the schedules are operator data and are edited in the console. If a job ever
looks wrong, fix the campaign document (console, or
`workspace/bin/console-schedule`) and let the reconciler apply it; editing the
cron by hand puts the two out of step until the next reconcile overwrites it.

A **second job firing the same campaign** is the one thing the reconciler will
not resolve by itself -- either could be the one that posts -- so it refuses and
asks for operator review. Check for one with the `cron list` above after any
rename or hand-created job.

Reconcile or redeploy **outside the campaigns' window**. A same-day second
firing is safe in itself: each delivered group is committed immediately, so the
re-run skips the groups it already reached.

Group membership and the per-group ad caps are NOT cron state either: they are
the `groups/<jid>` documents and `settings/defaults` in Firestore, so turning
the caps on (`caps_enabled`) is a console edit, not a deploy.

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
   Confirm `healthy`, the tested SHA, image digest, selected runtime flags, the connected main account,
   all jobs, and no `rollbackError`. A missing secret fails the job before any
   production mutation; provision it before the cutover to avoid a deployment gap.

The required WhatsApp account is `default` (the main line). It must remain enabled
in configuration and connected at startup. `ads` is optional for deployment and
rollback readiness, so its authentication outage no longer forces otherwise
healthy releases to roll back. Startup records retain `expectedAccounts` and
`connectedAccounts`, and also expose `requiredAccounts`, `disconnectedAccounts`
and `degraded`. An offline configured ad line therefore appears as `ready:true`,
`degraded:true`, `disconnectedAccounts:["ads"]`; this confirms deployment health,
not working advertising delivery. No account, credential, job, routing rule or
campaign definition is disabled or rewritten by this policy.

The same main-line requirement applies to structured rollback reports. Legacy
images with only an aggregate startup failure or unlabeled listener logs cannot
prove which account connected; their exact image can still be restored, but the
verifier fails closed rather than claiming main-line health from an ads listener.
Those old images cannot emit the new detailed readiness record. All releases
built with this policy report main-line readiness without waiting for ads.

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

As a backstop, the deploy job's *Wait for a quiet promo window* step
(`deploy/promo-deploy-window.py`) asks the gateway for its live campaign list
and holds the release while any enabled campaign may still be sending or starts
within 20 minutes. CI reads that list over the console API with
`CONSOLE_API_GATE_TOKEN` (below), not over `railway ssh`, which needs an SSH key
registered with the Railway account that a GitHub runner cannot have --
`RAILWAY_TOKEN` authenticates only the HTTP API. An operator runs
`./deploy/gateway-campaigns.sh` instead, which goes in over `railway ssh` and
names the project, service and environment explicitly (the same three ids
`deploy/railway-release.py` releases with, defined once in
`deploy/gateway_campaigns.py`), because a checkout need not be linked to one. "May still be sending" is the engine's
worst case for that campaign's current group count (full start jitter, the
longest gap between every send and the per-send allowance), capped at its job
timeout: about 69 minutes for a 46-group campaign. Reading the live list is also
what made console-created campaigns visible to this gate, which they were not
while it read committed files. If the gateway cannot be reached the gate refuses
rather than guesses. The same step reports the schedule guardrails and fails the
release when a campaign cannot finish inside its own timeout (see the repository
README). It waits up to 45 minutes; beyond that it fails and names the next
quiet time, and GitHub Actions → *Re-run failed jobs* after that time releases
the same commit. A deliberate restart during a run is a manual dispatch with
`ignore_promo_window`. The step sees only the committed manifest, not live
schedule drift or console-created campaigns, so it does not replace the checks
above. It exists because the 2026-11-09 08:31 merge of #184 restarted the
gateway at 08:42, inside fruit-trees-promo's 08:40 run, and with no automatic
catch-up fruit-trees posted to no group that day.

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

## WhatsApp stability and account recovery

The checked-in Railway deploy configuration sets `overlapSeconds: 0` and
`drainingSeconds: 360`, covering OpenClaw's 330-second shutdown budget. Keep one
replica and the native ownership lease; do not shorten the drain or manually clear
a live lease. The final privilege drop uses `setpriv` to exec the supervisor directly,
so `runuser` cannot cut its graceful shutdown short with its two-second kill timer. Scheduled standby remains enabled with its existing quiet-work guard.
The supervisor records planned standby, morning wake and platform shutdown signals;
the adapter records account-specific socket transitions using existing callbacks.
No new reconnect controller, timer, polling worker or watchdog setting is added.

Audit files live on the persistent volume at
`/home/joseph/.openclaw/runtime/whatsapp-health/events-YYYY-MM-DD.jsonl`. They contain
UTC timestamp, accountId, deploymentId, runId, connectionId, event, reason,
statusCode and planned. The journal is private (0600), retains 14 UTC days and is
capped at 1 MiB per day; failure or a full journal never stops the gateway.
`GENAIE_WHATSAPP_HEALTH_AUDIT=0` disables both adapter and supervisor audit writes.
Authentication failures and session conflicts are not phone restriction notices.

Before account recovery, inspect `openclaw channels status --json` through the
existing gateway helper. Its OpenClaw commands run as `joseph`, with
`HOME=/home/joseph` and `OPENCLAW_HOME` unset, matching the gateway's plugin
installation. Raw installer commands retain their existing privileged access.
Use **`--account ads`** on every ads login/logout command;
never omit the account and accidentally operate on `default`. Do not create a
standalone sender or concurrent login process for an already connected account.
For terminal invalid authentication, first preserve a private copy of its auth
directory, stop the affected listener with the account-specific logout command,
then perform one account-specific QR login and verify status. Logout deliberately
clears that account's credentials, so use it only for confirmed invalid authentication,
not transient reconnects. A phone scan is an operator action; do not retry QR login
automatically or send a test ad. A session conflict requires checking competing
owners before deciding whether relinking is necessary.

The ads trial pauses `let's build bindura` and `BULAWAYO ADVERTS`, leaving exactly
10 active ads groups and 29 configured campaign/group combinations. Campaigns check
existing listener status after start jitter; unavailable targets are not submitted
or marked sent. If an already-issued request loses its listener, its existing
uncertain-send receipt is preserved while remaining targets for the disconnected
account are deferred. Other accounts continue. Deferred work is not queued for a
catch-up burst. Record seven days of authenticated operation, including nightly
sleep/wake results and any separately captured phone restriction notice.

For the next round, ads runs **group broadcasts only**. The template and boot
reply-gate synchronizer enforce `channels.whatsapp.accounts.ads.dmPolicy="disabled"`
without disabling its listener. Native DM admission blocks automated replies,
typing, service actions and enquiry-triggered alerts, including administrator
and sibling-line DMs. Default customer replies remain open. Keep enquiry links
on default and retain the current groups, campaigns and standby schedule.
Deploy and verify the policy before a single ads relink if authentication is
invalid; keep regular WhatsApp on the phone for this test. Review after seven
days, recording manual replies and extra links as confounders. Do not
automatically resume replies: restoring the template policy to `"open"` and
deploying is a separate decision.

## Verify a deploy

```sh
railway logs        # expect: [whatsapp] ... starting provider, [gateway] ready,
                    #         [entrypoint] synced source overlay from /opt/live-source/workspace,
                    #         [entrypoint] public WhatsApp prompt updated|unchanged
                    #         [entrypoint] reply gates unchanged (12 paths already match the template)
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
replica and preserve the approved overnight standby schedule described below.

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
| `cache-trial` | `GENAIE_WHATSAPP_CACHE_CLEANUP=1` | Set it to `0` and redeploy |
| `standby-trial` | `GENAIE_OVERNIGHT_STANDBY=1` | Set it to `0` and redeploy |

`preserve` retains all six runtime settings, including catalog retirement.
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

### Expired WhatsApp metadata cleanup

`GENAIE_WHATSAPP_CACHE_CLEANUP=1` opts into a guarded adapter-load patch for
WhatsApp 2026.9.9 (`cache-trial` release profile). The release owns this setting,
checks it in startup verification and restores it during rollback.
Once per minute, each metadata owner's existing lifecycle
releases only entries whose original expiry has passed from its local,
reconnect and Baileys metadata caches. Fresh values, expiry times, metadata
fetching, mentions, reconnect fallback and account routing stay unchanged.
The unreferenced cleanup timer stops with its owner. No credentials, durable
messages, campaigns or features are removed. Unset the variable or set it to
`0` and redeploy to restore stock retention.

Tests exercise the pinned adapter's actual cache-owner code with its socket
replaced by a fixture, covering valid cache hits, expiry/refetch, reconnect
fallback, owner shutdown and account isolation. This establishes behavior,
not production RSS savings: V8 may retain freed pages for reuse. Measure a
comparable campaign and idle interval before attributing billing savings.

### Memory attribution: November 2, 2026

Use the operator report before proposing another memory change. It reads cgroup
counters, process RSS/PSS and the existing diagnostic recorder. It starts no
gateway during standby and installs no collector:

```sh
python3 deploy/railway-memory-report.py > /tmp/gateway-memory.json
python3 deploy/railway-cost-report.py --since 1h --memory > /tmp/gateway-baseline.json
```

The default report labels the recording timestamp and worker sample age. An idle
gateway can return old startup samples; never subtract them from current RSS to
infer native memory. Workers are grouped by their upstream diagnostic script
names, which do not necessarily identify a unique owner. RSS, PSS, container file
cache and V8 heap totals overlap. ArrayBuffers are already included in external
memory. Worker coverage excludes nested workers.

For fresh counters, explicitly opt into the bounded inspector probe:

```sh
python3 deploy/railway-memory-report.py --live-workers > /tmp/gateway-live-memory.json
```

This uses the pinned 2026.9.9 worker registry and public Node worker heap counters.
It opens a temporary loopback inspector, checks the gateway PID, samples counters
without pausing or dumping the heap, disconnects, and verifies port 9229 closes.
An existing inspector is left untouched. A failed probe is reported separately;
if cleanup could not be verified, inspect loopback port 9229. OS counters precede
the transient CLI/Node probes. Exclude their overhead from a billing comparison.
The installed-runtime integration test verifies inspector closure, real worker
heap sampling, and a subsequent SQLite-backed history read.

At 06:01 UTC, Railway reported $7.196443 in cycle-to-date usage: memory $6.511739
(90.49%), CPU $0.413485, egress $0.190896 and volume $0.080323, with negligible
backup charges. The cycle began October 20 at 18:50:31 UTC. OpenRouter/Vercel model
charges are separate. The current deployment already enabled `rpc-once`, catalog
and history retirement, expired-cache cleanup and overnight standby. November 1
overnight memory settled around 64 MB before the November 2 wake. These are
observations, not evidence for enabling the same optimizations again.

A fresh 06:20 UTC probe measured 1,308,618,752 bytes gateway RSS, 312,423,200 bytes
main heap used and 363,411,704 bytes across nine direct worker heaps. The history
and maintenance lanes retained two workers totaling 113,281,512 bytes of heap;
their request sequences were 316 and 230 despite both being idle at capture.
Usage read/refresh lanes had no workers. Shorter idle timers could churn those
repeatedly used workers. Disabling speculative history prewarm in an isolated
fixture saved about 18 MiB of worker heap but did not establish a worthwhile
whole-process reduction, so no prewarm or worker-policy change was shipped.

The operator report also supports a one-shot source-page-cache experiment:

```sh
python3 deploy/railway-memory-report.py --release-source-cache > /tmp/gateway-source-cache-trial.json
```

This asks Linux to release clean `.js`, `.mjs`, `.cjs` and `.json` pages under the
immutable installed OpenClaw `dist/` directory using `POSIX_FADV_DONTNEED`. It
does not delete or rewrite files, follow symlinks, touch databases/credentials,
release V8 objects, or invoke host-wide cache clearing. The scan has a 20-second
budget; errors and incomplete scans are reported. No timer or startup change
automatically repeats it. Future imports can read the same files normally.

A single 06:26 UTC production trial advised 9,296 files (309,926,241 source bytes).
After two seconds, file cache fell by 63,684,608 bytes (60.7 MiB), while container
memory fell by 58,601,472 bytes (55.9 MiB). Concurrent anonymous memory increased.
Resident source-page counts had substantially overstated reclaimable cache;
shared/mapped pages and kernel advisory behavior prevent assuming all resident
source is reclaimable or billed to this cgroup. This short, roughly 2.9% container
decrease does not meet the existing 5% sustained-cost acceptance criterion.
No automatic cleanup policy or new production worker patch is justified yet.

Capture matched wake, post-campaign and idle reports with the same deployment
and settings. Compare Railway raw metrics and actual full-week resource spend,
record campaign/CV workload and standby hours, and exclude deploy overlaps and
operator probes. Promote a candidate only after the existing full-week check
passes; do not infer monthly savings from these snapshots. Private raw reports
remain outside the repository.

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

## Changing a Railway variable

Every toggle below follows one pattern, because of three facts about the Railway
CLI this repo uses (5.71):

- **`railway variable set` deploys by itself.** Following it with `railway
  redeploy`, as these instructions used to, asks for a second deployment of the
  same change. Pass `--skip-deploys`, then run exactly one `railway redeploy --yes`.
  That also lets you choose when the gateway restarts -- inside a quiet window --
  rather than at the moment the value is typed.
- **`railway variables --unset` does not exist.** It fails with `unexpected
  argument '--unset'`. Switch a flag off by setting its off value (each block
  names it); remove a credential with `railway variable delete`.
- **`railway variable delete` has no `--skip-deploys`.** Check `railway deployment
  list` before redeploying after a delete, so you don't request a second
  restart on top of the one it may already have started.

`--yes` is required for `redeploy` outside a terminal, and it redeploys the image
already running rather than rebuilding.

## Turning replies off

```sh
railway variable set REPLIES_DISABLED=1 --skip-deploys && railway redeploy --yes   # replies off
railway variable set REPLIES_DISABLED=0 --skip-deploys && railway redeploy --yes   # replies back on
```

Only `1`, `true`, `yes` and `on` count as set (`sync-reply-gates.py` `truthy`), so
`0` is "replies on".

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

Do **not** trigger a manual ad send to test — each campaign delivers on its own
cron (confirm the live schedules with `./deploy/gateway-campaigns.sh`).

## Operator console API

Off unless explicitly enabled. What it serves and what it refuses is in the
repository README: [Operator Console API](../../README.md#operator-console-api).

```sh
railway variable set GENAIE_CONSOLE_API=1 \
  CONSOLE_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  --skip-deploys && railway redeploy --yes                          # console api on
railway variable set GENAIE_CONSOLE_API=0 --skip-deploys && railway redeploy --yes   # off
```

The entrypoint starts the API only when the value is exactly `1`.

`CONSOLE_API_TOKEN` is in the entrypoint's `SECRET_KEYS`, so it lands in the
volume `.env` at 0600 owner joseph and is never baked into the image. The server
refuses to start without one of at least 32 characters, so it cannot come up
unauthenticated. Neither variable is release-managed in `deploy/railway-release.py`,
so a deploy neither sets nor clears them; they behave like `REPLIES_DISABLED`.

The schedule route accepts an `operationId` alongside the usual
`changes`/`date`/`timezone`/`actor` body; that is an independent schedule entry
request, documented in the repository README:
[Independent schedule entries](../../README.md#independent-schedule-entries).
Nothing turns it on -- `writes.scheduleEditing.entries` follows
`workspace/bin/run-schedule-entry` shipping in the image, which
`deploy/stage-workspace.rsync` already stages with the rest of `bin/`. A deploy
that reaches a console whose Day timeline still asks for the companion bot
update is serving an image without that file.

It listens on `CONSOLE_API_PORT` (default 18790), never the gateway's 18789. The
entrypoint reassigns `PORT` to the gateway's port before any child starts, so a
process that read `$PORT` here would collide with the gateway; this is why the
console API names its own variable instead.

Confirm it is up from inside the container, which needs no public domain. The image
has no `curl`, so the probes use the container's Python:

```sh
railway ssh -- python3 -c 'import urllib.request as u; print(u.urlopen("http://127.0.0.1:18790/internal/console/health").read().decode())'
```

Reaching it from outside needs a Railway domain whose target port is
`CONSOLE_API_PORT`. That exposes only this API; 18789 stays private.

### The release gates' read-scoped token

The two release gates need one read -- the campaign list -- and must not be able
to change anything, while `CONSOLE_API_TOKEN` authorizes every route including
the writes and the account controls. So CI holds a second token instead:

```sh
railway variable set CONSOLE_API_GATE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  --skip-deploys && railway redeploy --yes                     # gate reads on
railway variable delete CONSOLE_API_GATE_TOKEN                  # gate reads off
```

Add the same value as the GitHub repository secret `CONSOLE_API_GATE_TOKEN`;
both deploy steps in `.github/workflows/deploy-main.yml` read it from there, and
each refuses to run without it rather than falling back to a path that cannot
work.

The API accepts it on `GET`/`HEAD /internal/console/campaigns` and refuses it
everywhere else: the other read routes, the `live/` views, every write and the
`accounts` routes, which matter most because a link session carries pairing QR
data. `GATE_ROUTES` in `console-api.py` is that list. It must be at least 32
characters and must differ from `CONSOLE_API_TOKEN`; the server refuses to start
otherwise, so a copy-paste cannot quietly hand the read-scoped holder every
write route. Rotate by setting a new value in both places, in either order --
the old token stops working when the gateway restarts.

Unlike `CONSOLE_API_TOKEN` it is not in the entrypoint's `SECRET_KEYS`: like the
write key it is handed to the console API child alone, so the gateway and the
campaign runs it spawns never see it.

### Enabling operator-config writes

Off unless a write key is installed, and separate from enabling the API at all.
What the write route does and refuses is in the repository README:
[Changing operator config from the console](../../README.md#changing-operator-config-from-the-console).

```sh
base64 -w0 write-key.json | railway variable set GCP_SERVICE_ACCOUNT_WRITE_B64 --stdin \
  --skip-deploys && railway redeploy --yes                             # writes on
railway variable delete GCP_SERVICE_ACCOUNT_WRITE_B64                  # writes off
```

`--stdin` keeps the key out of `railway`'s argv, where any local user could read it
from `ps` -- the same reason `gcp_token.mjs` takes its key on stdin.

Writes off removes the credential rather than blanking it. `delete` takes no
`--skip-deploys`, so check `railway deployment list` and redeploy only if it did
not already start one.

The key needs **Cloud Datastore User**, and it must be its own key: do not reuse
the seed key from your laptop, and do not widen the runtime key
(`GCP_SERVICE_ACCOUNT_B64`), which stays Cloud Datastore Viewer so no campaign
run can rewrite the config it is about to act on. Base64 for the same reason as
the runtime key: the entrypoint's sanitiser would mangle the key's newlines.

Unlike every other secret here, this one is **not** in `SECRET_KEYS`. That list
is exported into the shell that execs the gateway, so everything on it reaches
every cron run. The entrypoint captures this one, `unset`s it, and passes it to
the console API child alone; the boot log says which mode it came up in:

```text
console api enabled on port 18790 (config writes on)
console api enabled on port 18790 (read-only: no write key)
```

It is not release-managed, so a deploy neither sets nor clears it. Confirm the
state from inside the container with a read -- no write is needed to find out
whether writes would be accepted, because the overview reports it:

```sh
railway ssh -- python3 -c '
import json, os, urllib.request as u
r = u.Request("http://127.0.0.1:18790/internal/console/overview",
    headers={"Authorization": "Bearer " + os.environ["CONSOLE_API_TOKEN"]})
print(json.dumps(json.load(u.urlopen(r))["writes"], indent=1))'
```

`"enabled": true` means the console API holds the key. `"affectsProduction"` is
now always true: Firestore is the only thing a campaign run acts on, so a saved
change always reaches the campaigns. `fields` and `delete` are exactly what the
writer accepts; the console gates its controls on them.

A `dryRun` change is validated and reported without being written, which is the
safe way to check a key works.

The launch is backgrounded and never waited on, it owns no healthcheck, and the
server wraps its own entry point, so a fault can only kill the console API. Two
limits keep it from crowding the gateway it shares a container with, where the
hazard is not a crash but unbounded subprocess fan-out: at most two
`console-read` processes at a time, answering 503 beyond that, and successful
campaign/membership responses cached for 30 seconds. Account routes bypass that
cache and use one persistent, serialized gateway SDK bridge; overlapping account
requests return 409. The image must include `console_accounts.py` and stage the
new `workspace/lib/console_accounts.mjs`. Deploy the backend before enabling the
console controls. The gateway token stays inside the container; the local console
uses only `BOT_API_BASE_URL` and the matching `BOT_API_TOKEN`.

Firestore configuration inside those responses has a separate three-minute,
process-shared display cache at `$GENAIE_FIRESTORE_DIR/console-config.json`
(default `$OPENCLAW_STATE_DIR/runtime/firestore`). Private cache and lock files
live on the volume; credential/project hashes and invalidation generations prevent
reuse across contexts. Campaign, selected-group and settings reads have isolated
cache/snapshot files under `scopes/`, while all scopes watch the same mutation
marker. Background failures have a five-minute minimum cooldown, respect longer
upstream retry delays, and use
the validated snapshot with its original timestamp and 48-hour age limit. Metrics
and discovery remain on their existing schedules. Unchanged scheduler passes do
not clear response caches or rewrite campaign status.

The console can request `campaign/<slug>`, `campaign-groups/<slug>`, `group/<jid>`
or `groups-by-id/<comma-separated-jids>` instead of loading every group. Campaign
targeting uses the existing single-field array index on `groups.promos`; no new
composite index or data migration is needed. `settings` omits group and campaign
collections, `capabilities` needs no Firestore read, and `navigation` caches two
small count queries. `campaign-state/<slug>` and `captions/<slug>` read one campaign
document for save preflight. Campaign asset queries also filter by slug.

`POST /internal/console/config/<collection>/<id>` bounds a request body to 2048
bytes, or 64 KiB for `campaigns`. Only campaigns carry a large field -- captions,
which `operator_config.validate_captions` allows up to 60 KiB -- so the wider
envelope is theirs alone; for every other collection it is the only size limit
in the path. The refusal is `400 {"error":"Invalid config change."}`, sent before
console-write sees the request.

`POST /internal/console/config/batch/groups` accepts up to 100 selected group
changes with optional expected field values. The native transaction reads shared
validation rules once and commits changed groups with their audit entries
together; no-ops and dry runs write nothing. An uncertain commit is reported as
unknown and must be re-read before retrying.

`GET /internal/console/groups?fresh=1` (also supported by the scoped config routes,
overview, campaigns and preview, alongside `date`) forwards `--fresh-config`, bypasses both display
caches and refuses unavailable live config. Deploy this backend before the console
uses fresh reads for save validation. A Firestore 429 carries optional curated
`code: config_rate_limited`, `upstreamStatus: 429`, `quota` and
`retryAfterSeconds`; write refusals remain HTTP 503. Attempted writes invalidate
both caches even when an acknowledgement is lost. Fresh reads never fall back to
cached config to authorize a save, and the console never automatically resubmits
a config write.

Validate with `python3 workspace/lib/test_console_config.py`, the operator config,
writer, read bridge, scoped-read and API suites, then
`./deploy/ci-check.sh --base-sha <base-commit> --head-sha HEAD` to include the
deployment documentation contract. After backend and
console rollout, verify an intended tier edit appears immediately and compare
Firestore read usage across several page refreshes: the config cache's `checkedAt`
should advance at most once per three minutes per scope without writes or fresh reads. If
quota is already exhausted, verify after recovery; this change does not raise
quota or enable billing.

Pause and Unlink persist disabled state on the volume. Startup preserves this
state for default as well as ads. `REPLIES_DISABLED=1` still overrides Start;
after removing it, use Start explicitly. Existing reply policies remain intact.

The campaign routes need nothing from `deploy/`: every campaign is a Firestore
document, so the cron manifest and the mirror of the picture index that used to
be baked into the image are gone. The one file from `deploy/` the image still
carries is `deploy/r2_sigv4.py`, for signing R2 deletes during campaign
retirement.

## Automatic WhatsApp group inventory

The runtime image includes the group-discovery helper loaded by the existing
WhatsApp loader. It is enabled by default, uses each connected account's own
socket after connect and hourly, and needs no cron installation or Firestore
write credentials. Its timers stop with the gateway during overnight standby.

Inventory files live at `/home/joseph/.openclaw/runtime/whatsapp-groups/<accountId>.json`
on the volume, outside the image's workspace overlay. Keep these during runtime
cleanup. They are private metadata (IDs, group names and observation times),
not source files. Do not commit them or use them as advertising configuration.

The console Groups endpoint reports unassigned discoveries and scan health.
Entries older than two hours are stale; an offline account or overnight standby
can cause that normally. Failed lookups retain the last good observations and
emit sanitized `[whatsapp-groups]` warnings; they never restart a channel or
change campaign targeting. Set `GENAIE_GROUP_DISCOVERY=0` to disable scans while
retaining the inventory. Build/runtime compatibility checks fetch the exact
WhatsApp 2026.9.9 package before exercising its contracts in the networkless
test container. See the repository README's *Automatic group discovery* section
for behavior and the separate manual probe.

### Group activity and member totals

The hourly inventory also retains each group's participant total (provider
`size`, otherwise participant-array length) and `memberCountObservedAt`. It
retains no participant identities. Missing metadata keeps the previous total
with its original timestamp, so the console can label stale observations.

`whatsapp-group-activity.mjs` independently listens on each existing socket
before reply admission. DMC counts original incoming text/media over a rolling
24 hours, excluding bot accounts, protocol events, reactions, edits and history
synchronization. Group/message IDs deduplicate copies across accounts and
reconnects. No message body or member list is stored.

Keep `runtime/whatsapp-activity.sqlite` on the durable volume. It contains
minimal event metadata (including hashed senders for bot exclusion), collector
coverage intervals and bot identity hashes. Events and coverage expire after
48 hours; bot identity hashes persist across reconnects. The collector uses a
30-second heartbeat with a 90-second freshness limit, and disconnects, process
stalls and write failures leave gaps. Counts remain partial until the entire
rolling window is covered, including after deployment and overnight standby.
An unclean disconnect can take up to 90 seconds to become visible in coverage.

`GENAIE_GROUP_ACTIVITY=0` disables new activity collection independently of
metadata discovery. `[whatsapp-activity]` warnings indicate unavailable or
partial telemetry; failures never interrupt channel handling. The Groups API
adds read-only `metrics` to configured and discovered rows: nullable
`dailyMessageCount`, `memberCount`, `memberCountObservedAt`, ISO `windowStart`
and `asOf`, and `coverage` (`complete`, `partial`, `unavailable`). Counts always
describe the live rolling window, even for a historical campaign `date`.

Release this bot support before the paired console UI. Existing consoles ignore
the added fields; the new console displays `—` with an older bot. No historical
backfill, advertising tier changes or cap changes are involved.

## Human takeover of a customer chat

The operator runs the business from the same WhatsApp account as the bot, so a
reply typed by hand in a customer DM is outbound traffic. No gateway plugin can
see it, which is why the AI used to answer over the top of a conversation a
person was already having.

The pinned adapter does see it. `whatsapp-human-takeover.mjs` is injected by the
same loader as the group helpers, at the one place in inbound access control
that rejects a `fromMe` DM. By the time that branch runs, the adapter has
already dropped the echo of every message the bot itself sent (its
`recentOutboundMessages` cache, filled by `sendTrackedMessage`, with a 20 minute
TTL) and every receipt, reaction and deletion (`hasInboundUserContent`), so what
is left is a person typing. The peer is written to
`runtime/public-service-gate/takeovers.json` and nothing else happens there: the
helper never replies, never touches the socket, and a storage failure warns once
under `[whatsapp-takeover]` and leaves the bot talking.

Two rules keep a false positive from silencing a real customer. A message older
than the current connection is ignored, because a reconnect replays recent
traffic with a dedupe cache that may no longer vouch for it; the cost is a reply
typed while the gateway was offline, which is missed. And the operator's own
numbers are exempt on the plugin side, because an entry against the command line
would swallow `resume` itself.

`public-service-gate` reads that store (`lib/takeover.js`) and acts on it above
every other hook: the customer's later messages are claimed before the agent
runs, and a reply already on its way out is cancelled with
`cancelReason: human_takeover`, including one reply pacing is still holding. A
half-finished CV order is left untouched rather than cleared, so `resume` can
hand the interview back inside its 30 minute state window. The operator gets one
line per chat in the self-chat; `resume +<number>` releases a takeover and a
handoff together, and releases every spelling WhatsApp used for that chat.

Each file has a single writer, so no update can be lost if channels ever move
out of the gateway process: the adapter owns `takeovers.json`, the plugin owns
`handoffs.json`, and a `resume` is a stamped clear compared against the takeover
time rather than a delete. `GENAIE_HUMAN_TAKEOVER=0` switches off detection and
enforcement together, and leaves recorded chats recorded. An adapter upgrade
that moves any of the asserted anchors makes the patch throw, which is caught
and logged as `[whatsapp-takeover] detection unavailable`: the feature turns
itself off rather than breaking WhatsApp. The compatibility job asserts those
anchors and the echo/content ordering against the real 2026.9.9 package.

## Operator-requested group actions

The same loader that injects the group inventory also injects
`whatsapp-group-actions.mjs`, which holds each connected account's socket so the
console can ask for two things a config delete cannot do for itself: leave a
WhatsApp group, and forget the per-campaign send history for one. Both are
reached at `POST /internal/console/groups/<jid>/actions`, run through
`workspace/bin/console-group-action`, and need **no Firestore write
credentials** -- unlike the config route, which is gated on
`GCP_SERVICE_ACCOUNT_WRITE_B64`.

A leave is queued as a file under
`/home/joseph/.openclaw/runtime/whatsapp-group-actions/<accountId>/`, in
`requests/` and answered in `results/`, both named by the request's UUID -- a
group JID never becomes a path. Keep this directory during runtime cleanup while
the gateway is running; abandoned results are swept after ten minutes. A request
the monitor has not answered within the bridge's 25-second wait is reported as
unconfirmed and left queued, so a reconnecting account still performs it.

A successful leave prunes the group from that account's inventory file, because
the inventory is merge-only and would otherwise keep listing a membership that
no longer exists. The prune is timestamped in memory and a scan drops any group
whose lookup began before it, so a lookup still in flight at the moment of the
leave cannot write the departed group back -- nothing would ever remove it
again.

Clearing a group's records touches `runtime/cron-state/<name>/state.json` under
a `state.json.lock` file beside it, which `promo_broadcast` also takes when it
saves. Keep those lock files during runtime cleanup, or just let them be
recreated; they hold no state. Failures are reported in fixed words (`leave_failed`,
`leave_timeout`, `not_authorized`); provider errors are never forwarded.

`GENAIE_GROUP_ACTIONS=0` switches the surface off at both ends: the route answers
503 and the monitor's helper stays inactive. The injection is version-pinned like
the others and degrades to a `[whatsapp-group-actions]` warning rather than
blocking connect if the adapter's anchors move. `CONSOLE_GROUP_ACTION_BIN`
overrides the bridge path, as `CONSOLE_WRITE_BIN` does for writes.

## The operator data in Firestore and R2

Production has read its operator config from Firestore since 2026-11-08 and its
ad pictures from Cloudflare R2, and as of 2026-11-11 there is nothing else to
read: the committed roster, the generated target files, the cron manifest, the
baked images and the mirror of the picture index have all been deleted. What
each piece is and how a failure is handled is in the repository README:
[Where the operator config lives](../../README.md#where-the-operator-config-lives)
and [The ad images in Cloudflare R2](../../README.md#the-ad-images-in-cloudflare-r2).

`GENAIE_CONFIG_SOURCE` and `GENAIE_AD_ASSETS_SOURCE` were the cutover switches
and now select nothing. Leaving them set to `firestore` / `r2` is harmless;
setting either back to a retired value does **not** roll anything back, and the
resolver says so in its notes rather than failing the read. There is no
file-based rollback any more, which is the point of
`deploy/backup-operator-data.py` (see *Backups of the operator data* in the
repository README).

Two variables are still required:

```sh
base64 -w0 runtime-key.json | railway variable set GCP_SERVICE_ACCOUNT_B64 --stdin --skip-deploys
railway variable set \
  GENAIE_AD_ASSETS_BASE_URL=https://pub-63be390258b5461e931301ca810b7a86.r2.dev \
  --skip-deploys && railway redeploy --yes
```

`GCP_SERVICE_ACCOUNT_B64` is the read-only runtime key (Cloud Datastore
Viewer), base64-encoded so the entrypoint's sanitiser cannot mangle the key's
newlines. It is in the entrypoint's `SECRET_KEYS`, so it lands in the volume
`.env` at 0600 owner joseph and is never baked into the image; the cron runs
inherit it from the gateway's environment. Writes use the console API's own
`GCP_SERVICE_ACCOUNT_WRITE_B64` (Cloud Datastore User), kept to that one
process. Neither is release-managed in `deploy/railway-release.py`, so a deploy
neither sets nor clears them.

Without a readable key a campaign run falls back to the 48-hour snapshot in
`runtime/firestore/` and then, past that, sends nothing and exits non-zero so
the cron's failure alert fires. Check which it used on the next run:
`[operator-source]` and `[operator-config]` lines on the run's stderr, and
`./workspace/bin/console-read overview` reporting `configSource.source` as
`firestore` rather than `snapshot`.

The bucket is `r2.dev`-public and read anonymously, so the base URL is not a
secret; it is a variable rather than a constant so a custom domain can replace it
without a code change. Confirm it answers before the first flip -- a missing
object must come back `404`, not a redirect or a sign-in page:

```sh
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://pub-63be390258b5461e931301ca810b7a86.r2.dev/ads/cv/does-not-exist.png
```

`GENAIE_AD_ASSETS_BASE_URL` is not in the entrypoint's `SECRET_KEYS`, and does
not need to be: Railway service variables are already in the gateway's
environment and the cron runs inherit it. `SECRET_KEYS` exists for values
openclaw's own config interpolates. It is not release-managed in
`deploy/railway-release.py`, so a deploy neither sets nor clears it.

The index read uses the same read-only `GCP_SERVICE_ACCOUNT_B64` runtime key.
The `assets` collection is deliberately **not** in
`operator_config.COLLECTIONS`, so a malformed asset document cannot raise
`ConfigInvalid` across every campaign; `deploy/backup-operator-data.py` adds it
back explicitly, because a backup that skipped the picture index would not be
one.

**Before the first flip, probe the media root.** openclaw reads outbound local
media only from an allowlist. In `openclaw@2026.9.9` -- the version this image
pins, checked against that release's own `buildMediaLocalRoots` -- the list is the
temp dir, `<config>/media`, `<state>/media`, the delivery queue's media dir,
`<state>/canvas`, `<state>/workspace` and `<state>/sandboxes`. So `<state>/media`
is allowed and `<state>/runtime` is not, which is why the cache sits where it
does. That is a static read, though, and it cannot see a `tools.fs` setting on the
live config, so confirm it with one real send. Put any JPEG at the cache location
and send it to the bot's own number:

```sh
railway ssh -- runuser -u joseph -- mkdir -p /home/joseph/.openclaw/media/ad-assets
# upload or copy a probe.jpg there, then:
railway ssh -- runuser -u joseph -- openclaw message send --channel whatsapp \
  --target '<the bot's own JID>' --media /home/joseph/.openclaw/media/ad-assets/probe.jpg --json
```

A `messageId` in the reply is the gate. If it is refused as a local media read
outside the allowed roots, nothing downstream will work and the cache location
has to change before `r2` is flipped on.

**Before the first flip of a new bucket or cache location, probe the media root.**
openclaw reads outbound local media only from an allowlist. In
`openclaw@2026.9.9` -- the version this image pins, checked against that
release's own `buildMediaLocalRoots` -- the list is the temp dir,
`<config>/media`, `<state>/media`, the delivery queue's media dir,
`<state>/canvas`, `<state>/workspace` and `<state>/sandboxes`. So
`<state>/media` is allowed and `<state>/runtime` is not, which is why the cache
sits where it does.

Putting a new picture live needs no deploy and no Railway variable. From a
checkout, with the upload credentials in `~/.config/genaie/r2-upload.env` at
mode 600 (`R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY` from an R2 API token scoped to the one bucket with
Object Read & Write, plus `GCP_SERVICE_ACCOUNT_FILE` pointing at a Firestore
key with Cloud Datastore User -- never in the checkout, since `deploy/**` is
un-ignored):

```sh
deploy/upload-ad-image.py cv ~/new-cv-ad.png --replace cv-ad.png          # dry run
deploy/upload-ad-image.py cv ~/new-cv-ad.png --replace cv-ad.png --write
deploy/upload-ad-image.py --verify                                        # index vs bucket
```

None of those credentials ever becomes a Railway variable: only the operator's
machine signs anything, and the gateway only ever makes unauthenticated GETs.

## Retired daily job feeds: 2026-10-28

Vacancy Lamp and Hustlers Marketplace job posting and the shared scraping/cache
pipeline are removed. The ten promo campaigns and both WhatsApp accounts
remain required by release verification. Historical rollout notes above describe
the earlier feed-enabled deployments.

Before the removal deploy, save the feed definitions outside Git, verify neither
feed is running, and run `deploy/retire-job-scout-crons.sh --dry-run`, then run it
without the flag. This retires current and legacy feed names via the gateway CLI;
source deployment alone does not remove volume-backed cron registrations.
Deploy through the checked release workflow after the live cron preflight.
The boot overlay uses `rsync --delete` to remove the retired source and wrapper.
Verify the feed names are absent, both accounts connect and all ten campaigns
remain enabled. Do not send test adverts. Archive only
`runtime/job-scout/group-feed/` under a dated `retired/job-feeds/` directory on the
volume; retain cron receipts and all other runtime state. Restore the archived
cache, previous source image and saved cron definitions only if undoing retirement.
Memory savings require subsequent measurements and are not a release criterion.

## Approved overnight standby

The operator explicitly approved daily standby from **17:00 to 05:45
Africa/Algiers**, with WhatsApp replies and CV requests unavailable during that
window. `GENAIE_OVERNIGHT_STANDBY=1` enables the container-local Python supervisor;
`0` (image default) executes the original gateway command directly. The
`standby-trial` release profile enables only this flag and preserves other
runtime settings. `preserve` carries it forward; baseline disables it and exact
image rollback restores its prior value. This is a time schedule, never a
spending-triggered stop or a hard compute budget cutoff.

Every container starts the gateway once, even overnight, and requires a fresh
successful release-readiness acknowledgment before it can enter standby. The
entrypoint creates a private temporary directory, separate from persistent
state, shared by the supervisor and request tracker. The service gate records
only request counts, process ID and activity time; it records no customer data.
The supervisor checks gateway lanes, enabled cron running/next-run markers,
native running receipts, pending native/legacy deliveries, and the service-gate
request counter. Missing or unrecognized state keeps it awake. Any enabled promo
or feed due before the next wake also defers standby. Existing enabled cron
definitions are never changed; overnight maintenance follows upstream missed-run
behavior after startup.

After a quiet minute and successful checks, a drain marker holds new requests
with an availability reply. A second idle check at least a minute later permits
SIGTERM. Active work or a failed check removes that marker and defers shutdown;
there is no timeout that force-kills work to save money. The gateway's own
shutdown releases native state ownership. A tiny HTTP responder then serves
`/health`, `/healthz`, `/ready` and `/readyz` with HTTP 200 and explicit
`gatewayAvailable:false`; other routes return 503. It closes before the morning
gateway binds the same port. Wake uses the container's clock and Algeria timezone,
independently of a desktop automation. Unexpected child exits are failures so
Railway's existing restart policy applies.

The initial estimate was about $5.5 less compute for the remaining billing cycle,
based on prior overnight measurements. Supervisor overhead, pending-work delays,
future storage/network fees and actual daytime use affect the result. Validate
billed resource changes after a full overnight window; this does not guarantee a
$15 invoice. Keep the existing warning-only alert and do not add a hard cutoff.
Before activation, verify the connected main WhatsApp account and use the live
campaign preflight above. The operator subsequently approved deployments with
the advertising line offline: report it as degraded rather than blocking the
release or disabling its account or campaigns. This does not bypass the standby
activity guard; pending deliveries can still defer overnight shutdown.

### Intentionally paused campaigns

The `cv` campaign is paused: `enabled: false` on `campaigns/cv`, which leaves
its `cv-promo` cron job disabled on the gateway. Releases require only the
campaigns that are enabled. Customer CV requests remain available during normal
gateway hours. Correct the CV artwork contact before switching it back on, which
is a console edit -- the reconciler enables the job. A deployment never
registers or re-enables a cron job.


### Console-created campaigns

The console API accepts authenticated `POST /internal/console/campaigns/create`
requests when its existing write key is installed, with
`GENAIE_AD_ASSETS_BASE_URL` configured. Campaigns, group assignments, image
index, audit and idempotency receipt save in one Firestore transaction. Every
campaign is data-defined now, so a campaign created here and one converted from
a repository script differ only in the job name they run under.

The console signs direct, immutable R2 picture uploads using its own bucket-scoped
write token. The gateway needs no R2 write credentials. `console-campaign-create`
checks uploaded bytes before saving; `run-console-campaign` uses the shared
broadcast engine and the runtime read key. Caption overrides are also supported.

Desired schedules reconcile when the console API starts, after a campaign config
change, and every five minutes after a successful pass. Pending or failed passes
back off up to one hour. Reconciliation queries only campaigns with `kind=console`
and skips writes when `scheduleStatus` is unchanged.
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

## Console campaign retirement

Campaign deletion is opt-in: set `GENAIE_CAMPAIGN_DELETION=1` alongside
the existing console write key and scoped R2
cleanup credentials (`R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`). The entrypoint supplies R2 write credentials only to the
console API process and removes them before starting the gateway. The image
bakes the SigV4 cleanup helper (`deploy/r2_sigv4.py`), which is the one file from
`deploy/` it still carries.

`POST /internal/console/campaigns/<slug>/delete` uses the existing bearer token.
Acceptance is a transaction containing the retirement marker, audit snapshot and
cleanup receipt. The startup worker retries cleanup, waking immediately after
acceptance and checking pending work every minute. Converted jobs retain their
existing job name and shared-runner ownership. Uploads, installers and reseeding
cannot restore retired campaigns. Roll back console code independently while
retaining retirement-aware bot readers, runners and cleanup receipts. Deploying
this support does not itself retire any campaign.

## Console recurring schedule edits

The bot advertises `writes.scheduleEditing.edit` when live Firestore scheduling
support is installed. `POST /internal/console/schedule` uses the existing console
bearer token and write key; each accepted batch atomically saves desired times,
expected-revision checks and audit records. No additional deployment flags or
credentials are required for schedule editing.

The existing worker wakes after schedule writes, verifies the gateway job by
reading it back, and retries pending updates every minute after restart. Reads
separate saved desired times from last-confirmed times. Installers and drift
checks respect saved manifest overrides, converted campaigns retain their job
names, and script installers refuse to overwrite converted jobs. Conversion and
revert retain saved times, and reseeding refuses to replace console edits.

Deploy the bot before the console update. Old bots keep Save disabled with a
companion-update explanation. Local `console-schedule --apply` attempts immediate
application; run the console API worker for automatic pending retries. Neither
deployment nor changing a start time triggers `cron run` or a catch-up broadcast.

## Leased console display index

The runtime image installs `deploy/railway/requirements-console.txt` into
`/opt/console-python` and places that virtual environment first on PATH. The
portable validation job installs the same pinned Firestore client before tests.
This dependency supplies the public Listen RPC; campaign sends and writer
validation continue using their existing authoritative Firestore adapters.

Authenticated `/internal/console/live/*` routes share one display index on the
volume at the private Firestore state directory's `console-live.sqlite`. The
index watches settings, lines, campaigns, groups and campaign-deletion receipts.
It publishes only complete, consistent generations and persists resume positions
with document changes. Existing legacy routes remain available during rollout.

Visible console resources renew a 90-second lease and poll compact responses
at 30-second intervals. Expired leases stop the stream; returning viewers resume
it. A bootstrap or required reset may read the indexed collections once. List,
search, pagination and schedule requests use SQLite. Discovery and selected-group
metrics use local inventory/telemetry, without a configuration collection fetch.
Caption-editor requests load text/capabilities only; gallery requests are explicit.
Line configuration summaries and cap settings also use the index, while actual
WhatsApp connection status retains the independent gateway account API.

Release this bot version through the existing pipeline before the matching
console. Check `/internal/console/live/status` with the console bearer token:
`sync.state`, `sync.syncedAt`, `sync.lagSeconds` and `counters` report stream health,
bootstrap/rebuild work, active streams, indexed requests and returned rows.
Verify ten-row pages and that unchanged polls contain freshness metadata only.
Compare Firestore reads before/after ordinary viewing and searches; browser
requests should not trigger a new config scan. Writes still require live reads.
A post-write fence retains existing UI state until synchronization passes the
write boundary. Stale indexed displays never become targeting or save authority.
The fence requests an incremental stream resume to confirm the write promptly.
Its bounded confirmation wait does not hold the display projection lock, so
telemetry requests remain independent of Firestore availability. Delayed
revision-aware responses carry stale/unchanged metadata rather than old rows.
Roll back the console before the bot, retaining the database on the volume.

Queue-sweep test fixtures create both request and result directories before a
manual poll. Production startup creates them asynchronously and starts its own
poll only afterward; tests must not depend on the timing of that initialization.
Run `node --test deploy/railway/whatsapp-group-actions.test.mjs` alongside the
portable validation suite when changing those fixtures.
