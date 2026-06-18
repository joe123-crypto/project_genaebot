# Gmail Sender Onboarding

> **URL note:** App links use `APP_BASE_URL` (`https://agent-genaie.vercel.app`) from `~/.openclaw/.env` — the same source the account-link helper resolves. Do not use the ngrok dev tunnel in `/home/joseph/projects/agent_genaie/.env`; it is a stale local-dev value and returns 404. If a link fails, verify `APP_BASE_URL` in `~/.openclaw/.env` first.

Purpose: onboard a user so OpenClaw/Agent Genaie can send job scout or application emails from that user's own Gmail account after the user explicitly connects Gmail in the web app.

This is for the future Job Scout/Application service path. It does not make the public Job Scout service active by itself.

## Security Boundary

The agent may guide the user to the Agent Genaie web app and verify whether Gmail is connected.

The agent must not see, request, store, print, relay, or validate the user's Google credentials directly.

Allowed in chat:

- Give the Gmail connection link: https://agent-genaie.vercel.app/login?next=%2Fconnect-gmail
- Ask the user which email address they want to use as the sender.
- Explain that the user must sign in and approve Gmail access in the web app.
- Tell the user to check Spam or Junk if the sign-in email does not arrive.
- Ask the user to reply after the Gmail page shows Connected.
- Verify connected senders with `genaie-mail senders`.

Not allowed in chat:

- Asking for Gmail or Google passwords.
- Asking for OTPs, OAuth codes, backup codes, recovery codes, app passwords, private keys, or tokens.
- Asking for screenshots containing passwords, codes, or token values.
- Inspecting, printing, copying, or explaining Gmail token store contents.
- Revealing internal API keys, Firebase configuration, Google OAuth secrets, stack traces, logs, or implementation details to public users.
- Using `gog gmail send` for Agent Genaie product email.

Public replies must be short. Give the minimum useful information and the next action only.

## Onboarding Flow

```text
User asks to connect Gmail
Agent sends the Agent Genaie connect link
User signs in with the email address they want as sender
User clicks Connect Gmail
User approves Google Gmail send access
User replies when Gmail status says Connected
Agent verifies with genaie-mail senders
Agent can use that email with genaie-mail send --from
```

Only Firebase-registered users who have completed Gmail OAuth through their scoped `/{publicUserId}/connect-gmail` page can be used as senders. If the user's public app id is not known yet, send the login link with `next=/connect-gmail`; the app redirects to the scoped page after sign-in so the user does not have to find the Gmail tab manually.

## Public Agent Script

When a user asks to connect Gmail, reply:

```text
Open this link to connect Gmail:
https://agent-genaie.vercel.app/login?next=%2Fconnect-gmail

Sign in with the email address you want the agent to send from, then click Connect Gmail and approve Google access.

Do not send your Gmail password, OTP, or Google codes here. If the sign-in email does not arrive, check your Spam or Junk folder. Reply when Gmail shows Connected.
```

If the user asks why this is needed, reply:

```text
This lets the job scout send application emails from your own Gmail account after you approve access in Google.

Please connect it here:
https://agent-genaie.vercel.app/login?next=%2Fconnect-gmail
```

## Agent Verification

After the user says Gmail is connected, list connected senders:

```bash
/home/joseph/.openclaw/workspace/bin/genaie-mail senders
```

If the user's email appears in the list, reply:

```text
Your Gmail is connected. I can now use that email address as a sender when you approve an email task.
```

If the user's email does not appear, reply:

```text
I don't see that Gmail connected yet. Please reopen the link, sign in with the same email, and make sure Gmail status says Connected:
https://agent-genaie.vercel.app/login?next=%2Fconnect-gmail
```

## Sending As A Connected User

Use the connected sender email explicitly:

```bash
/home/joseph/.openclaw/workspace/bin/genaie-mail send \
  --from user@example.com \
  --to recipient@example.com \
  --subject "Subject" \
  --body-file path/to/body.txt \
  --force
```

For a non-sending check, use:

```bash
/home/joseph/.openclaw/workspace/bin/genaie-mail send \
  --from user@example.com \
  --to recipient@example.com \
  --subject "Subject" \
  --body "Test message" \
  --dry-run
```

## Failure Handling

Common setup failures:

- The user signed in to the app with one email but approved Google access for a different Gmail account.
- The user completed email-link login but did not click Connect Gmail.
- The user closed Google OAuth before approval finished.
- The Gmail scope was not granted.
- The user is registered in Firebase but has not connected Gmail yet.

For all of these, send the connect link again and ask the user to make sure Gmail status says Connected. Do not debug internals with public users.
