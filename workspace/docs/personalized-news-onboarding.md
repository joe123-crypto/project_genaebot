# Personalized News Onboarding

Purpose: onboard a public WhatsApp user for personalized news updates without creating per-user cron jobs or exposing internal implementation details in public chat.

## Public User Boundary

The public agent may collect and update the user's news preferences.

Allowed in public chat:

- Ask which news topics the user wants followed.
- Ask clarifying questions when the topics are too broad or unclear.
- Confirm that personalized news is set up for this WhatsApp chat.
- Stop personalized news when the user asks.

Not allowed in public chat:

- Using technical/internal terms such as `cron`, `registry`, `script`, `CLI`, `OpenClaw config`, logs, dispatcher, or implementation details.
- Sending public-user news digests to admin or WhatsApp groups.
- Revealing private user data, stored preferences for other users, raw command output, stack traces, or internal file contents.

Use 1-2 short sentences by default. Give only the answer or next action, use plain language, and never use em dashes.

## Current Operational Model

Personalized news uses one shared scheduled dispatcher. The dispatcher discovers registered users and sends each user a separate WhatsApp DM based on their saved topics.

Do not create one cron job per user.

Admin news scheduling is separate and must not be changed for public onboarding.

## Onboarding Flow

```text
Public chat -> user asks for personalized news
Agent -> asks for topics if missing
User -> sends topics
Agent -> saves topics for the requester's WhatsApp phone
Daily dispatcher -> sends personalized updates to that user's WhatsApp chat
```

## Public Agent Script

When a public user asks to start personalized news and has not provided topics, reply:

```text
Which topics should I follow?

For example: AI, football, business, or scholarships.
```

If the user provides topics in the first request, save them immediately.

If the user previously asked to start news and then replies with topics, save those topics.

## Save Or Update Topics

Use the requester's WhatsApp phone number. Do not use another phone number.

```bash
python3 /home/joseph/.openclaw/workspace/public-news/news_subscriptions.py register \
  --phone "<whatsapp phone>" \
  "<topic 1>" "<topic 2>" "<topic 3>"
```

Expected sanitized result:

```json
{
  "status": "ok",
  "phone": "<normalized phone>",
  "phone_hash": "<12-char phone hash>",
  "news": {
    "enabled": true,
    "topics": ["<topic>"],
    "frequency": "daily",
    "language": "English",
    "last_sent_at": null,
    "last_status": null
  }
}
```

After saving topics, tell the user:

```text
Personalized news is set for: <topics>. I'll send updates here.
```

## Stop Personalized News

When the user asks to stop, unsubscribe, cancel, disable, or turn off personalized news, run:

```bash
python3 /home/joseph/.openclaw/workspace/public-news/news_subscriptions.py disable --phone "<whatsapp phone>"
```

Then reply:

```text
Personalized news is off.
```

## Topic Guidance

Good topics are specific enough to search:

- AI tools
- football
- Algeria business
- Zimbabwe news
- scholarships
- crypto markets

If the user says only "news" or "anything", ask them to choose topics.

If the user gives too many topics, save the most important ones and keep the confirmation short.

## Failure Handling

If saving or stopping fails, do not show command output or internal errors. Reply:

```text
I couldn't save your news setup. Try again later.
```

For stop failures, reply:

```text
I couldn't stop personalized news. Try again later.
```

## Implementation Notes

`workspace/public-news/news_subscriptions.py` shares its phone normalization,
registry read/write/locking, and allowlist loading with the other public
workflows via `workspace/lib/` (`phone_utils`, `registry_store`, `allowlist`).
This is an internal refactor only: the CLI subcommands, arguments, and sanitized
output above are unchanged.
