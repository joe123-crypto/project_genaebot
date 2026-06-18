# Approved Commands

Use `openclaw-cap` for operational changes. Do not edit plugins, skills, cron
definitions, deployment files, or OpenClaw config by hand.

## Public WhatsApp Users

- `openclaw-cap users list`
- `openclaw-cap users add +213...`
- `openclaw-cap users remove +213...`
- `openclaw-cap users sync`
- `openclaw-cap users check`

## Account Linking

- `openclaw-cap account-link create-invite --phone +213... --purpose webetu`
- `openclaw-cap account-link status --phone +213...`
- Add `--dry-run` when you only need to verify the request shape.

## Cron Jobs

- `openclaw-cap cron run algeria-exhibitions --dry-run`
- `openclaw-cap cron run algeria-exhibitions`
- `openclaw-cap cron run github-issue-tracker`
- `openclaw-cap cron run webetu-meals --dry-run`
- `openclaw-cap cron run webetu-meals`

## Webetu

- `openclaw-cap webetu lookup-user --phone +263...`
- `openclaw-cap webetu check-credentials --phone +263...`
- `openclaw-cap webetu reserve --phone +263... --dry-run`

Report only user-facing status. Do not reveal internal IDs, credential source
fields, provider names, or raw command output containing sensitive data.
