# Local Event Scouting Onboarding

Purpose: onboard a public WhatsApp user for local event scouting without creating per-user cron jobs or exposing internal implementation details in public chat. Registered users get matching Algeria Exhibitions events sent to their WhatsApp DM first. Events are written into Google Calendar only after the user picks the ones they want, unless the user explicitly asks to auto-add every match.

## Public User Boundary

The public agent may link the chat, guide the user to connect Google Calendar access, collect optional event preferences, and register or stop event scouting.

Allowed in public chat:

- Send the Agent Genaie setup link created for this WhatsApp chat.
- Tell the user to sign in and connect Google with calendar access in the app.
- Show the available Algeria Exhibitions event categories before asking which kinds of events they want.
- Ask which available categories, location filters, and reminder timing they want.
- Confirm that event scouting is set up for this WhatsApp chat.
- Run an immediate scout after setup and show matching events as numbered choices.
- Add selected offered events to the requester's calendar when they reply with numbers.
- Stop event scouting when the user asks.

Not allowed in public chat:

- Asking for Google passwords, OTPs, OAuth codes, backup codes, private keys, or tokens.
- Asking for or revealing internal Firebase UIDs, API keys, secret values, Firestore document IDs, logs, command output, or file paths.
- Using technical/internal terms such as `cron`, `registry`, `script`, `CLI`, `OpenClaw config`, logs, or dispatcher.
- Sending other users' data, or writing to a calendar for a phone other than the requester's.

Public replies must use 1-2 short sentences by default. Give only the answer or next action, use plain language, and never use em dashes.

## Current Operational Model

Event scouting uses one shared scheduled dispatcher. It discovers registered users and sends matching upcoming events to each user's own WhatsApp DM as numbered choices. Calendar writes happen only after the user picks events, unless the user explicitly opts into auto-add mode.

Do not create one cron job per user.

The shared event-scout dispatcher is separate and must not be changed for public onboarding.

## Onboarding Flow

```text
Public chat -> user asks to scout local events
Agent -> checks if this WhatsApp chat is linked to an Agent Genaie login
Agent -> if not linked, sends the setup link for this chat
User -> signs in and connects Google with calendar access
Agent -> confirms calendar access is connected
Agent -> shows available Algeria Exhibitions categories
Agent -> asks event preferences (available categories, location, reminder timing)
Agent -> registers event scouting for the requester's WhatsApp phone in approval mode
Agent -> runs an immediate manual scout and shows numbered matching events
User -> replies with event numbers to save
Agent -> writes only selected events to that user's Google Calendar with reminders
Shared dispatcher -> later DMs new matching events as numbered choices
```

## Start Setup

First check whether this WhatsApp chat is already linked, using the requester's WhatsApp phone only:

```bash
/home/joseph/.openclaw/workspace/bin/account-link status --phone "<whatsapp phone>"
```

If the user is not linked, create or reuse a setup link for this chat and send it:

```bash
/home/joseph/.openclaw/workspace/bin/account-link create-invite \
  --phone "<whatsapp phone>" \
  --purpose events \
  --next-path /connect-gmail
```

```text
Sign in, connect Google Calendar, then reply when it's done:

<setup link>

Check Spam if the sign-in email doesn't arrive.
```

Stop here until the user confirms they have signed in and connected calendar access.

## Confirm Calendar Access

Re-run the status check to capture the linked account and confirm calendar access is connected. Use the connected account id from `account-link status` as the user id for registration; never show it to the user.

The user must have connected Google **with calendar access**. Users who previously connected only Gmail must reconnect to add calendar access on the current Agent Genaie app. If calendar access is not connected yet, ask the user to finish connecting in the app before registering.

## Show Available Categories

Before asking for event types, fetch the catalogue categories:

```bash
python3 /home/joseph/.openclaw/workspace/cron/event-scout-dispatcher/dispatch.py --list-categories
```

The output contains taxonomy-backed `categories` and `locations`. Show a short human list of the available category names, not raw JSON. If this command fails, do not use remembered or cached category lists and do not claim that a requested category is unavailable. Tell the user you cannot verify the catalogue categories right now and ask them to try again shortly.

If the user asks whether a category exists, run the category command again and answer only from that fresh output. If a requested category is not in the fresh category list, tell them the catalogue does not use that category and ask them to choose from the available categories. Do not register unavailable category filters.

## Collect Preferences

Ask one short question to capture filters and reminder timing. Category filters must come from the available catalogue categories. Empty categories mean all event types. Empty reminders mean the default reminders (1 day and 2 hours before).

```text
Which event types and locations should I watch? Reminders default to 1 day and 2 hours before.
```

## Register Event Scouting

Use the requester's WhatsApp phone number and the connected account id. Do not use another phone number.

```bash
python3 /home/joseph/.openclaw/workspace/public-events/event_subscriptions.py register \
  --phone "<whatsapp phone>" \
  --user-id "<connected account id>" \
  --category "<optional category>" \
  --location "<optional location>" \
  --reminder-min 1440 \
  --reminder-min 120 \
  --delivery-mode approval
```

`--category` and `--reminder-min` may be repeated; omit `--category`/`--location` to match all events; omit `--reminder-min` to use the default reminders (1440 and 120 minutes before). Use `--delivery-mode approval` unless the user explicitly asks for every matching event to be added automatically. If they explicitly ask for auto-add, use `--delivery-mode auto_add`.

Expected sanitized result:

```json
{
  "status": "ok",
  "phone": "<normalized phone>",
  "phone_hash": "<12-char phone hash>",
  "events": {
    "enabled": true,
    "user_id": "<connected account id>",
    "categories": ["<category>"],
    "location": "<location>",
    "reminder_minutes": [1440, 120],
    "delivery_mode": "approval",
    "synced_event_ids": [],
    "pending_offers": [],
    "last_synced_at": null,
    "last_status": null
  }
}
```

## Manual Scout After Setup

Immediately after registering, run a manual scout for this requester's phone:

```bash
python3 /home/joseph/.openclaw/workspace/cron/event-scout-dispatcher/dispatch.py \
  --phone "<whatsapp phone>" \
  --no-dm
```

Use the first item in the JSON result's `results` list to reply in WhatsApp. Do not show raw JSON or internal IDs.

If that result item has `status: "offered"`, show `added_offers` as a numbered list using each offer's `choice_number`, title, date range, location, category, and link. Then ask the user to reply with the numbers they want added.

```text
Event scouting is ready. I found these matches:

1. <event title> - <date range> - <location> - <category>
<link>

Reply with the numbers you want in your calendar.
```

If that result item has `status: "no_matches"`, say:

```text
Event scouting is ready. I'll message you when I find a match.
```

If that result item has `status: "needs-category-choice"`, show the available categories and ask the user to choose again.

If that result item has `status: "category-check-unavailable"`, say:

```text
I can't check event types right now. Try again in a bit.
```

## Save Selected Events

When the user replies with event numbers from a pending offer list, run:

```bash
python3 /home/joseph/.openclaw/workspace/cron/event-scout-dispatcher/dispatch.py \
  --phone "<whatsapp phone>" \
  --select "<number>"
```

Repeat `--select` for multiple numbers, or pass a comma-separated number list if that is easier.

If the result has `status: "saved"`, name the saved events:

```text
Added <event names> to your Google Calendar with reminders.
```

If the result has `status: "invalid_selection"`, say:

```text
Pick one of the event numbers I sent.
```

If the result has `status: "needs_selection"` and there are no pending events, say:

```text
There aren't any event choices waiting. Want me to check again?
```

If calendar access is no longer connected, ask the user to reconnect Google with calendar access in the app before adding events.

## Stop Event Scouting

When the user asks to stop, unsubscribe, cancel, disable, or turn off event scouting, run:

```bash
python3 /home/joseph/.openclaw/workspace/public-events/event_subscriptions.py disable --phone "<whatsapp phone>"
```

Then reply:

```text
Event scouting has been stopped for this WhatsApp chat.
```

## Failure Handling

If linking, registering, or stopping fails, do not show command output or internal errors. Reply:

```text
I couldn't set up event scouting. Try again later.
```

For stop failures, reply:

```text
I couldn't stop event scouting. Try again later.
```

## Implementation Notes

`workspace/public-events/event_subscriptions.py` shares its phone normalization,
registry read/write/locking, and allowlist loading with the other public
workflows via `workspace/lib/` (`phone_utils`, `registry_store`, `allowlist`).
This is an internal refactor only: the CLI subcommands, arguments, and sanitized
output above are unchanged.
