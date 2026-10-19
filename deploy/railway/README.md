# OpenClaw gateway — Railway deploy

The OpenClaw gateway (agent + internal cron + WhatsApp session) runs on **Railway**
(project `openclaw-gateway` / service `gateway` / environment `production`, EU West).
This directory holds the build that Railway ships. **This repo is the single source of
truth** — there is no hand-maintained `live-source/` copy anymore.

## How a deploy happens (the pipeline)

```
open PR ─► "Portable validation" (deploy-main.yml) runs deploy/ci-check.sh ─►
branch protection should block merge until green ─► merge to main ─►
Railway auto-builds deploy/railway/Dockerfile ─► gateway redeploys
```

**Railway deploys whatever lands on `main`, pass or fail.** CI only gates
production if the `Portable validation` check is *required* on `main`, which is
step 2 under one-time wiring below. Nothing in this repository can enforce it, so
verify it rather than assuming it:

```sh
gh api repos/joe123-crypto/project_genaebot/branches/main/protection \
  --jq '.required_status_checks.contexts'
```

An error or an empty list means a failing commit will deploy. If branch
protection is unavailable on the current plan, the enforced alternative is a
GitHub-Actions deploy job that runs `railway up` with
`needs: [validate, secrets]`, with Railway auto-deploy turned off.

- The **`validate` job** (`name: Portable validation`) in
  `.github/workflows/deploy-main.yml` runs the full suite on every PR and push
  (actionlint, shellcheck, gitleaks, `deploy/ci-check.sh`, npm audit). It is the check
  that should be required on `main` (see setup below). We reuse it rather than add a
  second workflow, so there is exactly one CI run per push.
  Its required checks cover repository and staging structure, documentation
  contracts, syntax, every discovered Python and Node test, ShellCheck, actionlint,
  locked npm dependencies, high-severity production npm advisories, and
  secret/private-state scanning. The Job Scout and channel-listener dependency sets
  are installed from their lockfiles before the Node tests run, then both
  production sets are audited. The Job Scout browser image is built when one of its
  inputs changes. Actions are pinned to commit revisions and containerized
  validation tools to versions.
- **Railway's GitHub integration** watches `main` and rebuilds on every push. It reads
  `railway.json` (repo root) → `deploy/railway/Dockerfile`.
- The Docker build assembles the curated overlay from repo HEAD in a first stage
  (`deploy/stage-openclaw-source.sh` + `deploy/verify-staged-source.sh`), bakes it at
  `/opt/live-source`, and the entrypoint syncs it onto the state volume at boot. No
  manual file copying, no drift.

To change what the gateway ships (workspace code, cron jobs like the CV-promo ad targets,
agent context markdown, the public WhatsApp system prompt, the business info page):
edit it in this repo, open a PR, merge. That's the whole flow. The prompt is the one
piece of `openclaw.json` the boot rewrites: `deploy/railway/sync-public-prompt.py`
(baked at `/usr/local/bin/`) copies `workspace/docs/public-whatsapp-system-prompt.txt`
into `channels.whatsapp.direct["*"].systemPrompt` just before the gateway starts.
The rest of `config/openclaw.template.json` is still not applied (see README).
`deploy/railway/wait-gateway-lease.py` (also baked at `/usr/local/bin/`) runs just
before the gateway and waits up to 330 s for a stale gateway owner lease left by a
killed container to expire, so a deploy that lands mid-cron-run does not crash-loop
(look for `[entrypoint] gateway owner lease held by another host` in the boot log).

Cron job definitions (schedule, command, job timeout) are the exception to "merge and
it ships": they live in the gateway's cron state on the volume and change only when
their `deploy/install-*.sh` installer is re-run (via `deploy/gateway-exec.sh`, i.e.
`railway ssh`). For example, the promo crons' 7200 s job timeout set in
`deploy/install-promo.sh` reaches production only after the eight
`install-*-promo.sh` installers run again; each installer now verifies the
timeout it registered is the one the gateway reports back, and the promo engine
reads that live value at run time so a not-yet-migrated job tightens its send
gaps rather than being killed with groups unreached (see README).

The same applies to the promo **schedules**, which moved off the hour (08:13,
08:40, 09:17, 10:35, 11:39, 12:50, 14:26, 17:06 `Africa/Harare`) so the account's
daily pattern is not perfectly periodic. Merging that change ships nothing on its
own. `deploy/promo-crons.json` is the declared cron state for all eight campaigns --
schedule, timezone, failure-alert target, command and the shared 7200 s job
timeout -- and `deploy/reinstall-promo-crons.sh` is the one command that compares
it to the gateway and applies it:

```sh
./deploy/reinstall-promo-crons.sh --check   # read-only: live state vs. what the repo declares
./deploy/reinstall-promo-crons.sh           # re-run all eight installers, then re-check
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
cannot leave the other seven unmigrated. `register_promo_cron` reads the schedule,
timezone, enabled flag and timeout back and fails loudly (printing the job's
current state) when any of them is not what it just set: nothing else would catch
a stale schedule, which is precisely what these installers exist to change.

Reconcile **outside the campaigns' window** -- before 08:13 or after 17:06
`Africa/Harare` (06:13 / 15:06 UTC; Harare has no DST) -- so no promo's schedule
moves while that promo is mid-run. A same-day second firing is safe in itself:
each delivered group is committed immediately, so the re-run skips the groups it
already reached.

Group membership and the per-group ad caps are NOT cron state: they live in
`workspace/cron/membership.json`, which ships with the workspace overlay on a
normal merge. Turning the caps on is a one-line edit to that file
(`defaults.caps_enabled`), not an installer run.

## One-time wiring (must be done in the dashboards; the CLI cannot do these)

1. **Railway → GitHub integration.** Railway dashboard → project `openclaw-gateway` →
   service `gateway` → Settings → *Source* → Connect Repo → `joe123-crypto/project_genaebot`,
   branch `main`. Install the Railway GitHub app on the repo if prompted. Leave *Root
   Directory* empty (repo root) so the build context includes `deploy/`, `config/`,
   `workspace/`. Confirm the existing volume stays mounted at `/home/joseph/.openclaw`.
   (Optional provenance: add a service variable `SOURCE_COMMIT=${{RAILWAY_GIT_COMMIT_SHA}}`
   — the Dockerfile records it in `/opt/live-source/.OPENCLAW_GENERATED`.)

2. **GitHub branch protection.** Require the `Portable validation` check on `main` and block
   direct pushes, so untested code can never reach Railway:

   ```sh
   gh api -X PUT repos/joe123-crypto/project_genaebot/branches/main/protection \
     -H "Accept: application/vnd.github+json" \
     --input - <<'JSON'
   {
     "required_status_checks": { "strict": true, "contexts": ["Portable validation"] },
     "enforce_admins": false,
     "required_pull_request_reviews": { "required_approving_review_count": 0 },
     "restrictions": null
   }
   JSON
   ```

   `required_pull_request_reviews` (with 0 approvals) routes every change through a PR so
   the check cannot be bypassed by a direct push to `main`; `strict: true` requires the
   branch be up to date. Leave `enforce_admins: false` so you retain a break-glass hotfix
   path (set it `true` to gate admins too). If classic branch protection isn't offered on
   the plan, the equivalent **repository ruleset** (Settings → Rules → Rulesets → require
   status checks + require a pull request) achieves the same and is available for free
   private repos.

## Break-glass: manual deploy without GitHub

If the integration is down and you must ship now, deploy this repo directly:

```sh
railway link            # openclaw-gateway / production / gateway
railway up --ci         # from the repo root; builds deploy/railway/Dockerfile
```

`railway up` uploads the repo root as the build context (honoring `.dockerignore`) and
runs the same Dockerfile, so the result is identical to an integration build.

> The old `/home/joseph/openclaw-deploy/` directory (with its own `live-source/`,
> `railway/Dockerfile`, `entrypoint.sh`) is **deprecated** — kept only as historical
> seed/volume tooling. Do not edit `live-source/` there; it no longer feeds production.

## Volume seeding & recovery

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
```

Do **not** trigger a manual ad send to test — the CV-promo campaign delivers on its own
cron at 11:39 Africa/Harare (declared in `deploy/promo-crons.json`; confirm the live
schedule with `./deploy/reinstall-promo-crons.sh --check`).
