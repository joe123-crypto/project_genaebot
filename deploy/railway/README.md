# OpenClaw gateway — Railway deploy

The OpenClaw gateway (agent + internal cron + WhatsApp session) runs on **Railway**
(project `openclaw-gateway` / service `gateway` / environment `production`, EU West).
This directory holds the build that Railway ships. **This repo is the single source of
truth** — there is no hand-maintained `live-source/` copy anymore.

## How a deploy happens (the pipeline)

```
open PR ─► "Portable validation" (deploy-main.yml) runs deploy/ci-check.sh ─►
branch protection blocks merge until green ─► merge to main ─► Railway auto-builds
deploy/railway/Dockerfile ─► gateway redeploys
```

- The **`validate` job** (`name: Portable validation`) in
  `.github/workflows/deploy-main.yml` runs the full suite on every PR and push
  (actionlint, shellcheck, gitleaks, `deploy/ci-check.sh`, npm audit). It is the required
  status check that gates merges into `main` (see setup below). We reuse it rather than add
  a second workflow, so there is exactly one CI run per push.
- **Railway's GitHub integration** watches `main` and rebuilds on every push. It reads
  `railway.json` (repo root) → `deploy/railway/Dockerfile`.
- The Docker build assembles the curated overlay from repo HEAD in a first stage
  (`deploy/stage-openclaw-source.sh` + `deploy/verify-staged-source.sh`), bakes it at
  `/opt/live-source`, and the entrypoint syncs it onto the state volume at boot. No
  manual file copying, no drift.

To change what the gateway ships (workspace code, cron jobs like the CV-promo ad targets,
agent context markdown): edit it in this repo, open a PR, merge. That's the whole flow.

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
     "required_pull_request_reviews": null,
     "restrictions": null
   }
   JSON
   ```

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
                    #         [entrypoint] synced source overlay from /opt/live-source/workspace
```

Do **not** trigger a manual ad send to test — the CV-promo campaign delivers on its own
cron at 10:00 Africa/Algiers.
