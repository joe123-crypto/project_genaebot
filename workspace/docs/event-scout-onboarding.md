# Local Event Scouting Onboarding

Purpose: onboard a public WhatsApp user for local event scouting without creating per-user cron jobs or exposing internal implementation details in public chat. Registered users get matching Algeria Exhibitions events written into their own Google Calendar with reminders.

## Public User Boundary

The public agent may link the chat, guide the user to connect Google Calendar access, collect optional event preferences, and register or stop event scouting.

Allowed in public chat:

- Send the Agent Genaie setup link created for this WhatsApp chat.
- Tell the user to sign in and connect Google with calendar access in the app.
- Ask which kinds of events they want (optional category/location filters) and how early they want reminders.
- Confirm that event scouting is set up for this WhatsApp chat.
- Stop event scouting when the user asks.

Not allowed in public chat:

- Asking for Google passwords, OTPs, OAuth codes, backup codes, private keys, or tokens.
- Asking for or revealing internal Firebase UIDs, API keys, secret values, Firestore document IDs, logs, command output, or file paths.
- Using technical/internal terms such as `cron`, `registry`, `script`, `CLI`, `OpenClaw config`, logs, or dispatcher.
- Sending other users' data, or writing to a calendar for a phone other than the requester's.

Public replies must be short. Give the minimum useful information and the next action only.

## Current Operational Model

Event scouting uses one shared scheduled dispatcher. It discovers registered users and writes matching upcoming events into each user's own Google Calendar with their chosen reminders.

Do not create one cron job per user.

The shared event-scout dispatcher is separate and must not be changed for public onboarding.

## Onboarding Flow

```text
Public chat -> user asks to scout local events
Agent -> checks if this WhatsApp chat is linked to an Agent Genaie login
Agent -> if not linked, sends the setup link for this chat
User -> signs in and connects Google with calendar access
Agent -> confirms calendar access is connected
Agent -> asks optional event preferences (types, location, reminder timing)
Agent -> registers event scouting for the requester's WhatsApp phone
Shared dispatcher -> adds matching events to that user's Google Calendar with reminders
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
To scout events for you, please open this link, sign in, and connect Google with calendar access:

<setup link>

Your sign-in email may land in Spam or Junk. Tell me once you've connected.
```

Stop here until the user confirms they have signed in and connected calendar access.

## Confirm Calendar Access

Re-run the status check to capture the linked account and confirm calendar access is connected. Use the connected account id from `account-link status` as the user id for registration; never show it to the user.

The user must have connected Google **with calendar access**. Users who previously connected only Gmail must reconnect to add calendar access on the current Agent Genaie app. If calendar access is not connected yet, ask the user to finish connecting in the app before registering.

## Collect Preferences (optional)

Ask one short question to capture optional filters and reminder timing. All are optional; empty means all events and default reminders (1 day and 2 hours before).

```text
Any specific kinds of events or a location to focus on? And how early should I remind you (for example 1 day and 2 hours before)?
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
  --reminder-min 120
```

`--category` and `--reminder-min` may be repeated; omit `--category`/`--location` to match all events; omit `--reminder-min` to use the default reminders (1440 and 120 minutes before).

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
    "synced_event_ids": [],
    "last_synced_at": null,
    "last_status": null
  }
}
```

After registering, tell the user:

```text
You're set up for event scouting. I'll add matching events to your Google Calendar with reminders.
```

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
I couldn't set up event scouting right now. Please try again later.
```

For stop failures, reply:

```text
I couldn't stop event scouting right now. Please try again later.
```
