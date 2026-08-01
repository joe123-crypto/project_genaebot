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
sets are audited. The self-hosted deploy checkout installs the same locked
dependencies before its mandatory validation pass. The production Job Scout
browser image is built when one of its inputs changes.

The deploy job has read-only repository permissions and runs only for a `main`
push after every hosted job succeeds. Actions are pinned to commit revisions;
containerized validation tools are pinned to versions. A successful deployment
records the exact production commit in the workflow summary.

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

The wrapper refuses dirty checkouts, unexpected remotes, stale commits, and
overlapping deploys. It requires the checkout to be owned by `joseph` and runs
Git fetch/revision checks as that user, allowing an Actions-injected credential
or Joseph's normal HTTPS credential helper to authenticate without exposing a
GitHub credential to root. It validates the exact SHA once, reconciles changed
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
