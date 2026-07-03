# OpenClaw GitHub Actions Deployment

This directory contains the host-side pieces for GitHub push-to-deploy.

Installed host files:

```text
/home/joseph/actions-runner-openclaw-prod/
/usr/local/sbin/openclaw-prod-deploy
/etc/sudoers.d/openclaw-prod-deploy
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

The runner must show online in GitHub with the `openclaw-prod` label before the
production job can run. Pull-request and portable validation jobs never use this
runner.

## CI Gates

Pull requests, `main` pushes, and manual workflow runs validate on GitHub-hosted
Ubuntu runners. Required checks cover repository/staging structure,
documentation contracts, syntax, all discovered Python and Node tests,
ShellCheck, actionlint, locked npm dependencies, high-severity production npm
advisories, secret/private-state scanning, and Docker Compose rendering. The
production Job Scout browser image is built when one of its inputs changes.

The deploy job has read-only repository permissions and runs only for a `main`
push after every hosted job succeeds. Actions are pinned to commit revisions;
containerized validation tools are pinned to versions. A successful deployment
records the exact production commit in the workflow summary.

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
overlapping deploys.

Because branch protection is unavailable for this private repository on the
current GitHub plan, CI cannot prevent a failing commit from entering `main`.
The job dependency prevents that commit from reaching production. Revert the
commit on `main` to roll production back through the normal validated path.
