# OpenClaw Local Deployment Source

This repository is the developer-owned layer around a local OpenClaw install.

It does not own or patch the installed OpenClaw npm package under
`/home/joseph/.npm-global/lib/node_modules/openclaw`. OpenClaw updates should
replace only package-managed files, while this repository tracks custom plugins,
commands, skills metadata, deployment helpers, and sanitized config templates.

Git is manual: review changes, commit intentionally, and push to the chosen remote
yourself. No cron job or deploy script should auto-commit or auto-push.
