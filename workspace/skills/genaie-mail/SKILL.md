---
name: genaie-mail
description: Send Gmail messages through the Agent Genaie app bridge.
---

# genaie-mail

Use `genaie-mail` for product/app Gmail sending from OpenClaw. It calls Agent Genaie's internal `/internal/gmail/send` endpoint. Pass `--from sender@example.com` to send as a registered user who connected Gmail in the app; omit `--from` to use the owner fallback.

Common commands
- List connected senders: `/home/joseph/.openclaw/workspace/bin/genaie-mail senders`
- Dry run: `/home/joseph/.openclaw/workspace/bin/genaie-mail send --from sender@example.com --to a@b.com --subject "Hi" --body "Hello" --dry-run`
- Send text: `/home/joseph/.openclaw/workspace/bin/genaie-mail send --from sender@example.com --to a@b.com --subject "Hi" --body "Hello" --force`
- Send file body with attachment: `/home/joseph/.openclaw/workspace/bin/genaie-mail send --from sender@example.com --to a@b.com --subject "Application" --body-file jobs/company/cover-letter.txt --attach /home/joseph/.openclaw/workspace/joseph-sibangani-munemo-cv.pdf --force`

Notes
- Requires `AGENT_GENAI_INTERNAL_API_KEY` and `AGENT_GENAI_MAIL_URL` in `/home/joseph/.openclaw/.env` or the process environment.
- Sender email must be a Firebase-registered user who has connected Gmail at `/connect-gmail`.
- Confirm before sending unless a trusted cron prompt explicitly authorizes auto-send.
- Do not use `gog gmail send` for Agent Genaie product email.
