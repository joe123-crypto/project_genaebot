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

The runner must show online in GitHub with the `openclaw-prod` label before
push-to-deploy can run.

## Deploy Flow

After the runner is online, production deploys happen when `main` changes:

```bash
cd /home/joseph/.openclaw
git push origin main
```

The workflow validates the exact pushed commit, then runs:

```bash
sudo /usr/local/sbin/openclaw-prod-deploy "$GITHUB_WORKSPACE"
```

The wrapper refuses dirty checkouts, unexpected remotes, stale commits, and
overlapping deploys.
