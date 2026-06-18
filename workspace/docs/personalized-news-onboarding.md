# Personalized News Onboarding

Purpose: onboard a public WhatsApp user for personalized news updates without creating per-user cron jobs or exposing internal implementation details in public chat.

## Public User Boundary

The public agent may collect and update the user's news preferences.

Allowed in public chat:

- Ask which news topics the user wants followed.
- Ask clarifying questions when the topics are too broad or unclear.
- Confirm that personalized news is set up for this WhatsApp chat.
- Stop personalized news when the user asks.
- Tell the user local event scouting registration is not active yet.

Not allowed in public chat:

- Using technical/internal terms such as `cron`, `registry`, `script`, `CLI`, `OpenClaw config`, logs, dispatcher, or implementation details.
- Claiming event scouting is active for public users.
- Sending public-user news digests to admin or WhatsApp groups.
- Revealing private user data, stored preferences for other users, raw command output, stack traces, or internal file contents.

Public replies must be short. Give the minimum useful information and the next action only.

## Current Operational Model

Personalized news uses one shared scheduled dispatcher. The dispatcher discovers registered users and sends each user a separate WhatsApp DM based on their saved topics.

Do not create one cron job per user.

Joe's existing admin `daily-tech-news` job is separate and must not be changed for public onboarding.

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
What topics should I follow for you?

Example: AI, football, business, scholarships, Zimbabwe news.
```

If the user provides topics in the first request, save them immediately.

If the user previously asked to start news and then replies with topics, save those topics.

## Save Or Update Topics

Use the requester's WhatsApp phone number. Do not use another phone number.

```bash
python3 /home/joseph/.openclaw/workspace/public-news/news_subscriptions.py \
  register \
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
You're registered for personalized news: <topics>.

I'll send updates to this WhatsApp chat.
```

## Stop Personalized News

When the user asks to stop, unsubscribe, cancel, disable, or turn off personalized news, run:

```bash
python3 /home/joseph/.openclaw/workspace/public-news/news_subscriptions.py \
  disable \
  --phone "<whatsapp phone>"
```

Then reply:

```text
Personalized news has been stopped for this WhatsApp chat.
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
I couldn't save your news setup right now. Please try again later.
```

For stop failures, reply:

```text
I couldn't stop personalized news right now. Please try again later.
```
