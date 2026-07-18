# Approved Admin Commands

Use the narrow `openclaw-cap` wrappers for routine service operations. General
shell access is reserved for explicit administrator requests.

## Job Scout (read-only status)

Answer subscriber/application questions from these commands, never from memory:

- `job-scout summary` — current ready-subscriber count + latest dispatch run counts
- `job-scout status --phone "+263..."` — one user's readiness and missing requirements
- `job-scout list-ready` — full list of currently ready subscribers
- `job-scout list-applications --user-id "<uid>"` — a user's recorded applications

Historical run stats: `runtime/job-scout/dispatch-runs/<run-id>.json`. Report
user-facing status only; do not reveal user ids, hashed keys, or raw records.
