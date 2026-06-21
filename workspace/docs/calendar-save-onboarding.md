# Save Events to Calendar

Purpose: when a user names specific events and asks to add them to their calendar
now (for example "save Startup World Cup Algeria and ICSMACS'26 in my calendar"),
write those exact events into that user's own Google Calendar with reminders,
without exposing internal implementation details in public chat.

This is **different** from event scouting (`docs/event-scout-onboarding.md`).
Event scouting registers the user for a shared scheduled dispatcher that only adds
Algeria Exhibitions catalog events. This flow writes the specific events the user
asked for, right now, through `bin/genaie-calendar`.

## Public User Boundary

The public agent may link the chat, guide the user to connect Google Calendar
access, and write the user's own requested events into their own calendar.

Allowed in public chat:

- Send the Agent Genaie setup link created for this WhatsApp chat.
- Tell the user to sign in and connect Google with calendar access in the app.
- Confirm which events were added.
- Add the specific events the requester named, into the requester's own calendar.

Not allowed in public chat:

- Asking for Google passwords, OTPs, OAuth codes, backup codes, private keys, or
  tokens.
- Asking for or revealing internal Firebase UIDs, API keys, secret values,
  Firestore document IDs, logs, command output, or file paths.
- Using technical/internal terms such as `cron`, `registry`, `script`, `CLI`,
  `OpenClaw config`, `bridge`, logs, or `sender uid`.
- Writing to a calendar for a phone other than the requester's, or for another
  user.

Public replies must be short. Give the minimum useful information and the next
action only.

## Flow

```text
Public chat -> user asks to save specific named events to their calendar
Agent -> checks if this WhatsApp chat is linked to an Agent Genaie login
Agent -> if not linked, sends the setup link for this chat
User -> signs in and connects Google with calendar access
Agent -> confirms calendar access is connected
Agent -> resolves each event's date (and time if relevant)
Agent -> writes each event into the user's own Google Calendar with reminders
Agent -> confirms which events were added
```

## Start Setup

First check whether this WhatsApp chat is already linked, using the requester's
WhatsApp phone only:

```bash
account-link status --phone "<whatsapp phone>"
```

If the user is not linked, create or reuse a setup link for this chat and send it:

```bash
account-link create-invite --phone "<whatsapp phone>"
```

```text
To add those to your calendar, please open this link, sign in, and connect Google
with calendar access:

<setup link>

Your sign-in email may land in Spam or Junk. Tell me once you've connected.
```

Stop here until the user confirms they have signed in and connected calendar
access.

## Confirm Calendar Access

Re-run the status check to capture the linked account. Use the connected account
id from `account-link status` as the user id below; never show it to the user.
Then confirm calendar access is connected:

```bash
genaie-calendar status --sender-uid "<connected account id>"
```

The result must show `"calendarConnected": true`. Users who connected only Gmail
must reconnect to add calendar access. If calendar access is not connected yet,
ask the user to finish connecting in the app before adding events.

## Resolve Each Event

For each event the user named, determine:

- A clear `summary` (the event name).
- The date(s). Use all-day dates (`YYYY-MM-DD`) unless a specific start/end time
  is known. If a date is unknown, resolve it (web search for the official event
  page) or ask the user one short question — do not guess a date.
- Optionally a `location` and a `source-url` (official event link).

Pick a stable `source-id` per event so re-adding the same event does not create a
duplicate: `adhoc:<slug-of-summary>-<start-date>`, e.g.
`adhoc:startup-world-cup-algeria-2026-06-24`.

## Write Each Event

For the requester's connected account id only:

All-day event:

```bash
genaie-calendar write \
  --sender-uid "<connected account id>" \
  --source-id "adhoc:startup-world-cup-algeria-2026-06-24" \
  --summary "Startup World Cup Algeria" \
  --start-date 2026-06-24 --end-date 2026-06-24 \
  --location "Algiers" \
  --source-url "https://example.com/event" \
  --reminder-min 1440 --reminder-min 120 \
  --force
```

Timed event (only when a specific time is known):

```bash
genaie-calendar write \
  --sender-uid "<connected account id>" \
  --source-id "adhoc:my-talk-2026-08-10" \
  --summary "My Talk" \
  --start-datetime 2026-08-10T14:00:00 --end-datetime 2026-08-10T16:00:00 \
  --timezone "Africa/Algiers" \
  --reminder-min 120 \
  --force
```

Notes:

- Use all-day (`--start-date`/`--end-date`) **or** timed
  (`--start-datetime`/`--end-datetime` + `--timezone`), not both.
- `--reminder-min` is repeatable; default to `1440` (1 day) and `120` (2 hours)
  before unless the user asks otherwise.
- A successful response has `"status": "created"` (or `"exists"` if already
  there). The `(account id, source-id)` pair dedups, so re-running is safe.

After writing, tell the user which events were added:

```text
Added Startup World Cup Algeria and ICSMACS'26 to your Google Calendar with
reminders.
```

## Failure Handling

If linking, checking calendar access, or writing fails, do not show command
output or internal errors. Reply:

```text
I couldn't add those to your calendar right now. Please try again later.
```

If only some events were added, name the ones that succeeded and offer to retry
the rest.
