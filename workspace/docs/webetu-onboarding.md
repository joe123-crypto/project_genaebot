# Webetu Progress Meal Reservation Onboarding

> **URL note:** App links use `APP_BASE_URL` (`https://agent-genaie.vercel.app`) from `~/.openclaw/.env` — the same source the account-link helper resolves. Do not use the ngrok dev tunnel in `/home/joseph/projects/agent_genaie/.env`; it is a stale local-dev value and returns 404. If a link fails, verify `APP_BASE_URL` in `~/.openclaw/.env` first.

Purpose: onboard a public WhatsApp user for Webetu Progress meal reservation through Agent Genaie without exposing the user's Webetu credentials to OpenClaw or the WhatsApp chat.

## Security Boundary

The normal onboarding path for all users is Agent Genaie account linking plus the Credentials Vault. Webetu credentials are stored as encrypted Firestore credential refs and are not handled through legacy credential systems at runtime.

The main/public agent must not see, request, store, print, relay, or validate the user's Webetu credentials directly.

Allowed in public chat:

- Send an Agent Genaie account-link setup URL created for this exact WhatsApp chat.
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

Public replies must be short. Give the minimum useful information and the next action only.

## Current Flow

```text
User asks to start Webetu
Agent checks whether this WhatsApp chat is linked to an Agent Genaie login
If not linked, agent creates an account-link setup URL for this phone
User opens the link, signs in by email, and is sent to the Credentials Vault
User saves Webetu credentials in the vault
Agent asks for and confirms the user's default restaurant by public name
  → If restaurant is not visible: inform user, offer Club 3 Bab Ezzouar or wait path
  → If user chooses to wait: advise manual reservations, tell user to return when ready
Agent runs sanitized credential and dry-run checks
Agent warns user about the 3-day wait window, offers to reserve now
If user agrees (or does not object): agent reserves meals for the next 3 days and reports results
Automatic runs fire on the 1st, 3rd, 6th, 9th, 12th, 15th, 18th, 21st, 24th, 27th, and 30th of each month at 12:00 PM Algiers, covering all days of the year with no gaps
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
Open this link and sign in with your email:
<setupUrl>

After sign-in, the app will open your Credentials Vault. Save your Webetu username and password there.

Do not send your Webetu username or password here. If the sign-in email does not arrive, check your Spam or Junk folder.
```

If the status result says `"linked": true`, send the vault link instead:

```text
Your WhatsApp chat is already linked.

Open your Credentials Vault here:
https://agent-genaie.vercel.app/<publicUserId>/vault

Save or update your Webetu username and password there. Do not send those details here.
```

If `publicUserId` is not available, use:

```text
https://agent-genaie.vercel.app/login?next=%2Fvault
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
I will use <restaurant name> as your default Webetu restaurant. Is that correct?
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
Your restaurant isn't showing on Webetu right now. This happens sometimes — not all restaurants appear at all times. It's beyond my control.

You have two options:
1. Use Club 3 Bab Ezzouar (the default) and I'll set up and reserve now.
2. Wait — I'll skip setup for now and you can make manual reservations until your restaurant shows up. When it does, message me and I'll set it as your default and fill your next 3 days.

Which do you prefer?
```

- If the user chooses option 1 (use default): save `set-default --restaurant "Club 3 Bab Ezzouar" --confirmed` and continue to the reservation step.
- If the user says anything like "wait", "skip for now", "I'll wait for my restaurant", "do not reserve yet": do NOT save a default restaurant and do NOT attempt any reservation. Reply:

```text
Understood. Make manual reservations on Webetu until your restaurant shows up. When you see it, message me and I'll set it as your default and fill your next 3 days.
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

This is a normal allowed user action, not an admin action. Never refuse it as admin-only or tell the user you need admin access — just run the checks below for the requester's own phone and reply with a short plain-language summary.

When a public user asks about their reservation status, upcoming meal, or whether their reservation is active, run these commands using only the **requesting user's own WhatsApp phone** — never another user's phone.

```bash
# 1. Confirm the user is linked
sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  lookup-user --phone "<whatsapp phone>"
```

If `registration_status` is not `"active"`, the user is not set up yet — redirect to onboarding.

```bash
# 2. Check their next scheduled reservation (no booking is made)
sudo -n -u webetu-worker /usr/local/bin/webetu-worker-safe \
  reserve --phone "<whatsapp phone>" --dry-run
```

Translate the result into plain language. Example reply:

```text
Your next meal reservation is set for <date> at <restaurant name>. All 3 meal types will be booked automatically.
```

Or if meals_reserved is 0 and the message explains why (e.g. already booked, no active dates):

```text
Your meals for <date> are already reserved at <restaurant name>.
```

Do not expose `user_id`, `source`, `status` codes, raw JSON, file paths, or worker internals to the user. Summarise in one or two short sentences.

## Override an Existing Reservation

A user may ask to redo or override a reservation that is already booked — for example, after changing their restaurant preference for a specific day.

**Date constraint:** Overrides are only valid for tomorrow or later. Never override a reservation for today or a past date — meals for today are already active. If the user asks for today, explain that today's reservation cannot be changed and offer to override tomorrow's instead.

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

Only run after the user confirms. Report success or failure from the worker output. Do not expose raw worker output — summarise: which date was overridden and how many meals were confirmed.

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

## Automatic Schedule (Next Run Lookup)

Whenever you tell a user when the next automatic run is, do NOT make the user calculate it and do NOT estimate "in 3 days". Look it up from this fixed table and state the actual date.

The job runs at 12:00 PM Algiers on these days of every month:

```text
1st, 3rd, 6th, 9th, 12th, 15th, 18th, 21st, 24th, 27th, 30th
```

To find the next run date: take today's date, then find the first day in the list above that is tomorrow or later. That is the date to tell the user (it always falls within the next 3 days, so it bridges the meals you just reserved with no gap).

Examples:

```text
Today June 11  → next run June 12
Today June 12 (after the noon run) → next run June 15
Today June 13  → next run June 15
Today June 30 (after the noon run) → next run July 1st
Today Jan 31   → next run Feb 1st
```

## Immediate Reservation After Setup

When the dry-run check passes and a default restaurant has been saved, warn the user before reserving:

```text
Setup is complete. I'll now try to reserve your meals for the next 3 days at <restaurant name>.

If you'd prefer I don't reserve right now, say so. The next automatic run is on <look up the next fire date from the Automatic Schedule table above, e.g. "June 12"> at 12:00 PM — if you skip now, you'll need to make manual reservations until then.
```

If the user says anything like "don't reserve", "wait", "do it later", "I'll do it manually", or any instruction to pause — respect it immediately without reserving:

```text
Understood. I won't reserve now. Make reservations manually until the next scheduled run. When you're ready for me to take over, just say so.
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
Done. Your meals are reserved for the next 3 days at <restaurant name>. The next automatic run is on <look up the next fire date from the Automatic Schedule table above>.
```

If some fail:

```text
I reserved meals for <date1> and <date2>, but <date3> failed. Please reserve <date3> manually.
The next automatic run is on <look up the next fire date from the Automatic Schedule table above>.
```

If no default restaurant was saved (user chose to wait for their restaurant): skip this section entirely — the user was already advised to make manual reservations.

## Failure Handling

Common setup failures:

- The user has not opened the account-link setup URL yet.
- The user signed in but did not save Webetu credentials in the vault.
- The user saved incorrect Webetu credentials.
- The WhatsApp phone number used in helper commands does not match the requester.
- Gmail/email-link login landed in Spam or Junk.

For failures, send the relevant setup link again and ask the user to complete the next visible app step. Do not inspect secrets, print internal command output, or perform broad system repair.
