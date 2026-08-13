# Webetu Progress Meal Reservation Onboarding

> **URL note:** App links use `APP_BASE_URL` (`https://www.genaie.site`) from `~/.openclaw/.env`, the same source the account-link helper resolves. Do not use the ngrok dev tunnel in `/home/joseph/projects/agent_genaie/.env`; it is a stale local-dev value and returns 404. If a link fails, verify `APP_BASE_URL` in `~/.openclaw/.env` first.

Purpose: onboard a public WhatsApp user for Webetu Progress meal reservation through Agent Genaie without exposing the user's Webetu credentials to OpenClaw or the WhatsApp chat.

## Security Boundary

The normal onboarding path for all users is Agent Genaie account linking plus the Credentials Vault. Webetu credentials are stored as encrypted Firestore credential refs and are not handled through legacy credential systems at runtime.

The main/public agent must not see, request, store, print, relay, or validate the user's Webetu credentials directly.

Allowed in public chat:

- Send an Agent Genaie account-link setup URL created for this exact WhatsApp chat.
- Send that setup URL exactly as returned, in full and on its own line. The `token=...` in it is the user's own one-time sign-in code, not a secret to hide: never mask, shorten, or alter it, and never replace the token with asterisks. A masked link cannot sign anyone in.
- Ask which restaurant the user wants as their default Webetu restaurant.
- Offer to show supported restaurant names if the user is unsure.
- Repeat the selected restaurant back to the user and save it only after the user confirms it.
- Tell the user to sign in with email and save Webetu credentials in the Credentials Vault.
- Explain that Webetu credentials must be entered only in the web app, not in WhatsApp.
- Tell the user to check Spam or Junk if the sign-in email does not arrive.
- After the user says setup is complete, run sanitized worker checks that do not print secrets.

Not allowed in public chat:

- Asking for the Webetu username, registration number, or password.
- Asking for screenshots of credentials.
- Asking for restaurant IDs, depot IDs, residence IDs, wilaya IDs, API values, or other internal Webetu fields.
- Asking for OTPs, Google codes, Firebase IDs, recovery data, private keys, API keys, or tokens.
- Printing raw helper output that contains internals, stack traces, file paths, config values, or implementation details.
- Running broad repair, migration, credential-store discovery, cron, service, or config changes for a public onboarding request.

Use 1-2 short sentences by default. Give only the answer or next action, use plain language, and never use em dashes.

## Current Flow

```text
User asks to start Webetu
Agent checks whether this WhatsApp chat is linked to an Agent Genaie login
If not linked, agent creates an account-link setup URL for this phone
User opens the link, signs in by email, and is sent to the Credentials Vault
User saves Webetu credentials in the vault
Agent runs sanitized credential and dry-run checks
Agent asks for and confirms the user's default restaurant by public name
  → If restaurant is not visible: inform user, offer Club 3 Bab Ezzouar or wait path
  → If user chooses to wait: advise manual reservations, tell user to return when ready
Agent offers to reserve the next 3 days now
If user agrees (or does not object): agent reserves meals for the next 3 days and reports results
Automatic reservations are stopped. Users must make future reservations manually.
```

## Start Setup

Use only the requester's WhatsApp phone number.

First check whether the phone is already linked:

```bash
/home/joseph/.openclaw/workspace/bin/account-link status --phone "<whatsapp phone>"
```

If the result says `"linked": false`, create a Webetu account-link invite:

```bash
/home/joseph/.openclaw/workspace/bin/account-link create-invite \
  --phone "<whatsapp phone>" \
  --purpose webetu \
  --next-path /vault
```

Send the returned `setupUrl` to the user:

```text
Sign in here and save your Webetu details in the Credentials Vault:
<setupUrl>

Don't send those details here. Check Spam if the sign-in email doesn't arrive.
```

If the status result says `"linked": true`, send the vault link instead:

```text
Save or update your Webetu details here:
https://www.genaie.site/<publicUserId>/vault
```

If `publicUserId` is not available, use:

```text
https://www.genaie.site/login?next=%2Fvault
```

## Restaurant Preference

Restaurant selection happens only after the user has saved Webetu credentials in the Agent Genaie Credentials Vault. The live restaurant list is fetched by the isolated Webetu worker after it logs into Webetu/ONOU for that user.

Restaurant selection is critical: if the wrong restaurant is used, the user may not be able to eat there. Never save a restaurant preference until the user clearly confirms it.

Ask one simple question:

```text
Which restaurant should I use by default for your Webetu meal reservations?
```

If the user is unsure, offer the supported list:

```text
I can show you the supported restaurants so you can choose one.
```

List supported restaurant names:

```bash
/home/joseph/.openclaw/workspace/bin/webetu-preferences list-restaurants \
  --phone "<whatsapp phone>"
```

Show only the public restaurant names and meal availability from the result. Do not mention IDs, depot IDs, residence IDs, wilaya IDs, API values, worker internals, or database fields.

After the user chooses, confirm before saving:

```text
Use <restaurant name> as your default restaurant?
```

Only after the user confirms, save it:

```bash
/home/joseph/.openclaw/workspace/bin/webetu-preferences set-default \
  --phone "<whatsapp phone>" \
  --restaurant "<confirmed restaurant name>" \
  --confirmed
```

If the helper rejects the restaurant, tell the user it is not supported yet and offer the supported list again. Do not guess a restaurant mapping.

## Restaurant Not Found

If the user's intended restaurant does not appear in the list returned by `list-restaurants`, inform them plainly:

```text
Your restaurant isn't showing on Webetu right now.

1. Use Club 3 Bab Ezzouar (the default) and I'll set up and reserve now.
2. Wait and make reservations yourself until your restaurant appears.

Which option do you want?
```

- If the user chooses option 1 (use default): save `set-default --restaurant "Club 3 Bab Ezzouar" --confirmed` and continue to the reservation step.
- If the user says anything like "wait", "skip for now", "I'll wait for my restaurant", "do not reserve yet": do NOT save a default restaurant and do NOT attempt any reservation. Reply:

```text
Make reservations yourself until your restaurant appears. Message me when it does.
```

Do not attempt to probe, retry, or repair the restaurant list. The intermittent availability is a Webetu API issue outside the agent's control.

Tell the user they can later ask to use another supported restaurant for one specific day:

```text
You can also ask me later to use a different supported restaurant for a specific day.
```

For a one-day override, confirm the date and restaurant before saving:

```bash
/home/joseph/.openclaw/workspace/bin/webetu-preferences set-override \
  --phone "<whatsapp phone>" \
  --date YYYY-MM-DD \
  --restaurant "<confirmed restaurant name>" \
  --confirmed
```

Existing users may not have a saved default restaurant yet. That is not an error; the worker keeps using the current fallback restaurant for backward compatibility.

## Check Reservation Status (User Request)

This is a normal allowed user action, not an admin action. Never refuse it as admin-only or tell the user you need admin access. Run the checks below for the requester's own phone and reply with a short plain-language summary.

When a public user asks about their reservation status, upcoming meal, or whether their reservation is active, run these commands using only the **requesting user's own WhatsApp phone**, never another user's phone.

```bash
# 1. Confirm the user is linked
sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  lookup-user --phone "<whatsapp phone>"
```

If `registration_status` is not `"active"`, the user is not set up yet. Redirect to onboarding.

```bash
# 2. Check their next reservation date (no booking is made)
sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  reserve --phone "<whatsapp phone>" --dry-run
```

Translate the result into plain language. Example reply:

```text
Your meals for <date> at <restaurant name> aren't reserved yet. Please make the reservation manually.
```

Or if meals_reserved is 0 and the message explains why (e.g. already booked, no active dates):

```text
Your meals for <date> are already reserved at <restaurant name>.
```

Do not expose `user_id`, `source`, `status` codes, raw JSON, file paths, or worker internals to the user. Summarise in one or two short sentences.

## Override an Existing Reservation

A user may ask to redo or override a reservation that is already booked, for example after changing their restaurant preference for a specific day.

**Date constraint:** Overrides are only valid for tomorrow or later. Never override a reservation for today or a past date because meals for today are already active. If the user asks for today, explain that today's reservation cannot be changed and offer to override tomorrow's instead.

Look up the user's internal ID first:

```bash
sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  lookup-user --phone "<whatsapp phone>"
```

Use the `user_id` from the result. Then run the override reservation for the requested date:

```bash
sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  reserve --user "<user_id>" --date YYYY-MM-DD --override
```

The `--override` flag bypasses the "already booked" skip and re-submits the reservation to the ONOU API, which replaces the existing booking.

Confirm with the user before running:

```text
I'll override your reservation for <date>. This will replace your existing booking for that day. Shall I go ahead?
```

Only run after the user confirms. Report success or failure from the worker output. Do not expose raw worker output. Summarise which date was overridden and how many meals were confirmed.

## After The User Saves Credentials

When the user says they saved credentials in the vault, validate only that user's WhatsApp phone through the isolated worker.

Check the registration row:

```bash
sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  lookup-user --phone "<whatsapp phone>"
```

Expected sanitized result should have `"source": "firestore"`. Do not expose source/provider details to the public user.

Then run the sanitized credential check:

```bash
sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  check-credentials --phone "<whatsapp phone>"
```

This checks whether encrypted credentials can be loaded without printing them.

Then run a dry-run reservation check:

```bash
sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  reserve --phone "<whatsapp phone>" --dry-run
```

This authenticates privately, discovers/caches metadata, checks existing reservations, and reports what would be reserved without creating a reservation.

## Immediate Reservation After Setup

When the dry-run check passes and a default restaurant has been saved, warn the user before reserving:

```text
Setup is complete. Should I reserve the next 3 days at <restaurant name> now?
```

If the user says anything like "don't reserve", "wait", "do it later", "I'll do it manually", or any instruction to pause, respect it immediately without reserving:

```text
I won't reserve now. Please make your reservations manually.
```

If the user agrees or does not object, run reservations for the next 3 days using the requester's WhatsApp phone:

```bash
sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  reserve --phone "<whatsapp phone>" --date $(TZ="Africa/Algiers" date -d "tomorrow" +%Y-%m-%d)

sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  reserve --phone "<whatsapp phone>" --date $(TZ="Africa/Algiers" date -d "2 days" +%Y-%m-%d)

sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  reserve --phone "<whatsapp phone>" --date $(TZ="Africa/Algiers" date -d "3 days" +%Y-%m-%d)
```

Report results in plain language (date + outcome). Do not expose raw JSON, user IDs, status codes, or worker internals.

If all 3 succeed:

```text
Done. Your meals are reserved for the next 3 days at <restaurant name>.
```

If some fail:

```text
I reserved meals for <date1> and <date2>, but <date3> failed. Reserve <date3> yourself.
```

If no default restaurant was saved because the user chose to wait for their restaurant, skip this section entirely. The user was already advised to make manual reservations.

## Failure Handling

Common setup failures:

- The user has not opened the account-link setup URL yet.
- The user signed in but did not save Webetu credentials in the vault.
- The user saved incorrect Webetu credentials.
- The WhatsApp phone number used in helper commands does not match the requester.
- Gmail/email-link login landed in Spam or Junk.

For failures, send the relevant setup link again and ask the user to complete the next visible app step. Do not inspect secrets, print internal command output, or perform broad system repair.
