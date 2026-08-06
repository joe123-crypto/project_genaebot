# OpenClaw GitHub Actions Deployment

This directory contains the host-side pieces for GitHub push-to-deploy.

Installed host files:

```text
/home/joseph/actions-runner-openclaw-prod/
/usr/local/sbin/openclaw-prod-deploy
/etc/sudoers.d/openclaw-prod-deploy
/etc/systemd/system/actions.runner.*.service.d/override.conf
/swap-openclaw.img
```

The workflow is:

```text
.github/workflows/deploy-main.yml
```

## One-Time Receiver Bootstrap

Before enabling `job_scout_release` dispatches, install the request-aware
wrapper from a reviewed `main` checkout. The workflow never replaces its own
privileged entrypoint:

```bash
cd /home/joseph/.openclaw
sudo ./deploy/github-actions/install-openclaw-prod-deploy-wrapper.sh
sudo cmp deploy/github-actions/openclaw-prod-deploy /usr/local/sbin/openclaw-prod-deploy
```

The installer requires the existing narrow sudoers file, validates it with
`visudo`, rejects `NOPASSWD: ALL`, and installs the wrapper as
`root:root 0755`. Future runner registration also runs this installer. The
sudoers rule must authorize only `/usr/local/sbin/openclaw-prod-deploy` with
arguments; it must not authorize direct access to deploy scripts, Python,
Docker, or a shell.

The same bootstrap creates, without truncating an existing file, the root-owned
successful-release ledger:

```text
/var/lib/openclaw-deploy/job-scout-production-ledger.jsonl
```

It remains outside every application runtime directory and is `root:root 0600`.

The Job Scout release images are **public** GHCR packages owned by
`joe123-crypto/genaie-job-scout`, so the receiver pulls them **anonymously** and
no cross-repository package grant is required. The receiver must **not**
`docker login ghcr.io` before pulling: this repository's `GITHUB_TOKEN` carries
no grant on another repository's packages, and once docker is authenticated GHCR
scopes the pull token to this identity's empty permissions and answers
`denied: denied` for the public images instead of serving them over the
anonymous public read path. The `job-scout-release` job therefore logs out of
GHCR before pulling to guarantee the anonymous path.

## Activate The Runner

Get a short-lived runner registration token from:

```text
GitHub repo -> Settings -> Actions -> Runners -> New self-hosted runner
```

Then run:

```bash
cd /home/joseph/.openclaw
sudo RUNNER_TOKEN='...' ./deploy/github-actions/register-openclaw-runner.sh
```

Provision the persistent 8 GiB deployment swap file once on the production
host. The command is idempotent and refuses unexpected targets or sizes:

```bash
sudo ./deploy/github-actions/ensure-openclaw-swap.sh
```

Runner registration installs a systemd drop-in that restarts a failed runner
and kills the complete runner cgroup after an OOM, preventing orphaned deploy
shells from continuing a partial deployment. Reapply it independently with:

```bash
sudo ./deploy/github-actions/install-openclaw-runner-hardening.sh --restart
```

The runner must show online in GitHub with the `openclaw-prod` label before the
production job can run. Pull-request and portable validation jobs never use this
runner.

## CI Gates

Pull requests, `main` pushes, and manual workflow runs validate on GitHub-hosted
Ubuntu runners. Required checks cover repository/staging structure,
documentation contracts, syntax, all discovered Python and Node tests,
ShellCheck, actionlint, locked npm dependencies, high-severity production npm
advisories, secret/private-state scanning, and Docker Compose rendering. The
Job Scout and ZimLinks listener dependency sets are installed from their
respective lockfiles before Node tests run, then both production dependency
sets are audited. Both self-hosted deploy jobs — the ordinary `main` deploy and
the dispatched Job Scout release deploy — install the same locked dependencies
before handing off to `openclaw-prod-deploy`, because that wrapper re-runs
`deploy/ci-check.sh` on the production host and its Node tests import
`playwright` and `baileys` from those trees. `actions/checkout` wipes untracked
paths, so a previous run's `node_modules` never carries over. The production Job
Scout browser image is built when one of its inputs changes.

The ordinary deploy job has read-only repository permissions and runs only for
a `main` push after every hosted job succeeds. Job Scout releases additionally
use a read-only Actions permission to correlate the dispatch; the release images
are public and pulled anonymously, so no package grant or registry login is
used. Actions are pinned to commit revisions; containerized validation tools are
pinned to versions.

## Job Scout Release Receiver

The receiver accepts only `job_scout_release`. Its payload has exactly these
string fields:

```text
jobScoutSha, bundleDigest, browserDigest, listenerDigest,
manifestHash, sourceRunId, correlationId
```

The three artifact values must be distinct lowercase `sha256:` digests. Mutable
tags and caller-supplied registry names are rejected; references are assembled
under `ghcr.io/joe123-crypto/genaie-job-scout`. A hosted job pulls all three
public images anonymously by digest, verifies OCI source/revision/artifact
labels, and safely extracts the
non-running bundle without starting it. It binds the canonical manifest
repository, commit, worker references, and raw-byte SHA-256 to the payload, then
runs the WS4 command:

```bash
python3 deploy/job_scout_consumer.py validate "$BUNDLE_DIR"
```

The bundle is unpacked with `docker create` + `docker export`, which writes a
*container* filesystem rather than the image filesystem. That tar therefore also
carries files the container runtime injects into every container: `/etc/mtab`
(a **symlink** to `/proc/mounts`), `/.dockerenv`, and empty `/dev`, `/proc`,
`/sys`, `/etc`. None of it belongs to the release, so the extraction is scoped
to the release payload:

```bash
python3 deploy/job_scout_release.py safe-extract \
  --archive "$BUNDLE_TAR" --destination "$BUNDLE_DIR" \
  --only job-scout-manifest.json --only workspace
```

Out-of-scope members are ignored, never written, so no runtime file reaches a
deploy stage. Scoping does not relax any check: traversal, absolute paths, and
control characters are still rejected for every member, and a symlink or other
non-regular file *inside* the scope still fails the release. The self-hosted
transport extracts its own `pack-candidate` tar with no `--only`, keeping the
fully strict behaviour there.

Only the resulting same-run Actions artifact can reach the self-hosted job. The
self-hosted job verifies the hosted candidate tar SHA-256, safely extracts it
to preserve declared executable modes, recomputes the request hash, and invokes
the installed wrapper
with the checkout, request, request hash, and paired bundle. The wrapper checks
paths, ownership, permissions, symlinks, hashes, receiver SHA, manifest binding,
and the WS4 consumer again under the host-wide lock. A rollback must also match
the Job Scout SHA, manifest hash, and all three artifact digests in an existing
hash-chained host ledger entry. The wrapper appends a new entry only after the
deployment and health path succeeds. It forwards
`JOB_SCOUT_BUNDLE_DIR` internally; the workflow does not rely on `sudo`
preserving environment variables.

Successful deploys publish a 90-day `job-scout-production-<manifest-hash>`
ledger artifact containing the exact manifest and normalized request. Manual
rollback accepts only that 64-character manifest hash. The hosted job must find
an unexpired ledger from a successful `main` workflow run and repull all three
immutable artifacts. The UI has no bundle or worker digest inputs.
The hosted ledger is a discovery mechanism; it cannot authorize a privileged
rollback when the matching successful release is absent from the host ledger.

Before dependency installation and again before production mutation, the host
must have at least 1 GiB of `MemAvailable` and 6 GiB of combined available
memory plus free swap. A failed preflight prints the largest resident-memory
consumers and exits before staging or restarting production.

## Deploy Flow

After the runner is online, production deploys happen when `main` changes:

```bash
cd /home/joseph/.openclaw
git push origin main
```

The hosted jobs validate the exact pushed commit. After they pass, the
self-hosted job runs:

```bash
sudo /usr/local/sbin/openclaw-prod-deploy "$GITHUB_WORKSPACE"
```

For a hosted-validated Job Scout release, the workflow uses the constrained
request form:

```bash
sudo /usr/local/sbin/openclaw-prod-deploy "$GITHUB_WORKSPACE" \
  --job-scout-request "$REQUEST_FILE" "$REQUEST_SHA256" "$BUNDLE_DIR"
```

The wrapper refuses dirty checkouts, unexpected remotes, stale commits, and
overlapping deploys. It requires the checkout to be owned by `joseph` and runs
Git fetch/revision checks as that user, allowing an Actions-injected credential
or Joseph's normal HTTPS credential helper to authenticate without exposing a
GitHub credential to root. Root-run child checks receive an exact, command-scoped
Git safe-directory value for that owner-checked checkout; neither user's global
Git configuration is changed. It validates the exact SHA once, reconciles changed
container inputs without force-recreating unchanged services, waits for two
successful gateway API probes, then runs health and cron reconciliation.

The same wrapper is the supported manual fallback from the normal checkout:

```bash
cd /home/joseph/projects/genaebot
sudo /usr/local/sbin/openclaw-prod-deploy "$PWD"
```

The source-controlled health check skips Webetu entirely when its public
feature flag is disabled. When Webetu is enabled, its external dependency
health remains advisory and Firestore quota exhaustion is reported concisely.
Core gateway/container/API failures remain hard failures.

Only a fully completed deploy updates:

```text
/home/joseph/openclaw-deploy/.last-successful-deploy.json
```

The marker contains the successful Git SHA, completion time, and staged-content
digest. Failed or canceled deploys leave the previous successful marker intact.

Because branch protection is unavailable for this private repository on the
current GitHub plan, CI cannot prevent a failing commit from entering `main`.
The job dependency prevents that commit from reaching production. Revert the
commit on `main` to roll production back through the normal validated path.
