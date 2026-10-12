---
name: genaie-calendar
description: Write events into a connected user's Google Calendar through the Agent Genaie app bridge.
---

# genaie-calendar

Use `genaie-calendar` to write events into a specific user's Google Calendar from OpenClaw. It calls Agent Genaie's internal `/internal/calendar/write` endpoint, which uses the app-owned Google OAuth connection that user set up in the app. Each user gets events in their **own** calendar; target a user with `--sender-uid <Firebase UID>` (resolve it from the requester's WhatsApp phone via `account-link status --phone`). Do not use `gog` for product/app calendar writes.

Always check that the user has connected calendar access before writing, and only write events for the requester's own user id.

Common commands
- Check calendar access: `/home/joseph/.openclaw/workspace/bin/genaie-calendar status --sender-uid <uid>` → look for `"calendarConnected": true`.
- Dry run (no write, no network call): `/home/joseph/.openclaw/workspace/bin/genaie-calendar write --sender-uid <uid> --source-id adhoc:my-event-2026-08-10 --summary "My Event" --start-date 2026-08-10 --end-date 2026-08-10 --reminder-min 1440 --reminder-min 120 --dry-run`
- All-day write: `/home/joseph/.openclaw/workspace/bin/genaie-calendar write --sender-uid <uid> --source-id adhoc:my-event-2026-08-10 --summary "My Event" --location "Oran" --source-url "https://example.com" --start-date 2026-08-10 --end-date 2026-08-10 --reminder-min 1440 --reminder-min 120 --force`
- Timed write: `/home/joseph/.openclaw/workspace/bin/genaie-calendar write --sender-uid <uid> --source-id adhoc:my-talk-2026-08-10 --summary "My Talk" --start-datetime 2026-08-10T14:00:00 --end-datetime 2026-08-10T16:00:00 --timezone Africa/Algiers --reminder-min 120 --force`

Flags
- `--sender-uid` selects the connected user (required for product writes). `--source-id` is a stable dedup key; the bridge dedups on `(senderUid, sourceId)`, so re-running the same event will not create a duplicate.
- Use all-day (`--start-date`/`--end-date`, YYYY-MM-DD) **or** timed (`--start-datetime`/`--end-datetime` RFC3339 + `--timezone`), not both. `--location`, `--source-url`, and repeatable `--reminder-min` (popup minutes before start) are optional.
- `--dry-run` prints a sanitized payload and makes no network call. `--force` is required to actually write.

Notes
- Requires `AGENT_GENAI_INTERNAL_API_KEY` and `APP_BASE_URL` (or `AGENT_GENAI_CALENDAR_URL`) in `/home/joseph/.openclaw/.env` or the process environment. Do not print the key.
- The user must have connected Google **with calendar access** in the Agent Genaie app. Users who connected only Gmail must reconnect to add calendar access.
- Do not use `gog calendar` for Agent Genaie product calendar writes.
