# Job Scout Onboarding

Purpose: onboard a public WhatsApp user for Job Scout without exposing internal implementation details or asking for private Gmail credentials in chat.

## Public Boundary

Allowed in public chat:

- Send the Agent Genaie setup link created for this WhatsApp chat.
- Tell the user to sign in and connect Gmail in the app.
- Offer two setup paths: guided questions or CV autofill.
- Ask only one or two onboarding questions per message.
- Ask the user to upload their CV in this WhatsApp chat.
- Read/extract CV details only after the user explicitly permits it.
- Confirm that Job Scout will use this WhatsApp chat for updates.
- Give the scam-safety warning and require acknowledgement before saving the final Job Scout profile.

Not allowed in public chat:

- Asking for Gmail passwords, OTPs, OAuth codes, backup codes, private keys, or tokens.
- Asking for internal Firebase UID, API keys, secret values, Firestore document IDs, logs, command output, or file paths.
- Sending another user's jobs, CV, preferences, or Gmail sender details.
- Promising applications before the app account is linked, Gmail is connected, a CV/profile is saved, and the scam warning is acknowledged.
- Overwhelming the user with all setup questions in one message.

Use 1-2 short sentences by default. Use plain language, and never use em dashes.

## Onboarding Flow

```text
User asks for Job Scout
Agent checks whether this WhatsApp chat is linked to an Agent Genaie login
If not linked, agent creates an account-link URL for the requester's WhatsApp phone
User signs in to Agent Genaie with their email
Setup link binds this WhatsApp chat to that app user
User connects Gmail in the app
Agent asks whether the user wants guided questions or CV autofill
Agent collects target roles, a preferred location that includes its country, and other details gradually, one or two questions per message
Agent summarizes the profile and asks for corrections
Agent sends scam warning and requires acknowledgement
Agent saves profile through job-scout helper
Daily dispatcher finds ready users and applies for matching jobs
```

## Start Setup

Use the requester's WhatsApp phone number only.

First check whether this WhatsApp chat is already linked to an Agent Genaie login:

```bash
/home/joseph/.openclaw/workspace/bin/account-link status --phone "<whatsapp phone>"
```

If the status says `"linked": false`, create an account-link invite for Job Scout:

```bash
/home/joseph/.openclaw/workspace/bin/account-link create-invite \
  --phone "<whatsapp phone>" \
  --purpose jobs \
  --next-path /connect-gmail
```

Send the returned `setupUrl` to the user:

```text
Sign in here to link this chat:
<setupUrl>

Check Spam if the sign-in email doesn't arrive.
```

Stop here until the user has opened the link and signed in. Do not collect CV/profile details before this WhatsApp chat is linked.

If the status says `"linked": true`, create or reuse the Job Scout setup path:

```bash
/home/joseph/.openclaw/workspace/bin/job-scout create-invite --phone "<whatsapp phone>"
```

Send the returned `setupUrl` to the user with this public text:

```text
Connect Gmail here:
<setupUrl>

1. Answer a few short questions.
2. Upload your CV so I can use its details.
```

## Setup Option Choice

If the user has not chosen a setup path, ask:

```text
How do you want to set up Job Scout?

1. Answer a few questions.
2. Upload your CV.
```

Do not continue to CV extraction unless the user clearly chooses option 2 and gives permission.

## Guided Question Flow

Ask one or two questions per message, then wait for the user to answer before asking the next question.

Recommended order:

1. What kind of jobs should I look for?
2. Which city or area should I search? Include the country so I target the right place, for example: Bindura, Zimbabwe.
3. What qualifications or certificates do you have?
4. What work experience should I mention?
5. What skills should I mention in applications?
6. Are there any jobs you do not want?
7. Is there anything else I should remember when applying?
8. Upload your CV.

If the user already answered some of these, do not ask again. Ask only for missing or unclear details.

Friendly field mapping:

- Jobs to look for -> `--target-role` (repeat for multiple roles)
- Preferred city or area plus country -> `--location` in `City or area, Country` format (repeat only for locations in the same country)
- Search country -> derived by `resolve-location`; Algeria (`dz`) remains the fallback only when no location is supplied outside onboarding
- Search language -> `--language`; it controls the JSearch language and query wording (`à` for French, `in` otherwise), with `fr` as the default
- Qualifications or certificates -> `--qualification`
- Work experience -> `--experience`
- Useful skills -> `--skill`
- Jobs to avoid -> `--exclusion`
- Anything else to remember -> `--prompt-note`
- Uploaded CV -> `--cv`

When the user uploads the CV, immediately stage it. This uploads the PDF to the
Agent Genaie Cloudflare R2 bucket (the phone must already be linked):

```bash
/home/joseph/.openclaw/workspace/bin/job-scout stage-cv \
  --phone "<whatsapp phone>" \
  --cv "<MediaPath>"
```

`stage-cv` returns a `cvFileRef` that is the R2 object key (`<uid>/cv/cv.pdf`),
not a local path. Use it for later extraction and saving. The CV must be a PDF.
Do not mention internal storage keys to the user.

Before confirming or saving the profile, validate every preferred location:

```bash
/home/joseph/.openclaw/workspace/bin/job-scout resolve-location \
  --location "Bindura, Zimbabwe"
```

If validation fails, ask the user to restate the location with its country. Do not guess the country or silently use the Algeria fallback. Multiple locations must resolve to the same country because JSearch accepts one country filter.

## CV Autofill Flow

If the user chooses CV autofill, ask for explicit permission before reading/extracting from the CV:

```text
Upload your CV and say, "Use my CV for Job Scout."
```

After the user uploads the CV and gives permission:

- Immediately stage the CV. This uploads the PDF to the Agent Genaie R2 bucket:

```bash
/home/joseph/.openclaw/workspace/bin/job-scout stage-cv \
  --phone "<whatsapp phone>" \
  --cv "<MediaPath>"
```

- Use the returned `cvFileRef` (R2 object key `<uid>/cv/cv.pdf`) for CV extraction and final profile saving.
- Extract likely job types, qualifications, work experience, and skills from the CV.
- Ask follow-up questions only for missing or unclear details.
- Ask at most one or two questions per message.
- Do not invent details not supported by the CV or the user's answers.

Before saving, summarize what was understood:

```text
Here's what I understood:

Jobs to look for: ...
Preferred location: ...
Qualifications: ...
Experience: ...
Skills: ...
Jobs to avoid: ...
Notes for applications: ...

What should I change?
```

Save only after the user confirms or corrects the summary.

## Scam Warning Consent

Before saving the final Job Scout profile, send this warning:

```text
Never pay upfront for an application, interview, medical check, training, uniform, or job placement. Genaie can't guarantee every listing is legitimate.

Reply "I understand" to continue.
```

Rules:

- Do not save the profile until the user clearly acknowledges the warning.
- Accept clear acknowledgements such as "I understand", "I agree", "understood", or equivalent.
- If the user refuses, stop onboarding politely and do not save the profile.
- If the user is unsure, explain briefly that they should never pay money upfront, then ask whether they understand and want to continue.

## Save Profile

After the user has provided enough details, uploaded a CV, confirmed the summary, and acknowledged the scam warning, save the profile:

```bash
/home/joseph/.openclaw/workspace/bin/job-scout save-profile \
  --phone "<whatsapp phone>" \
  --cv "<cvFileRef returned by stage-cv>" \
  --target-role "General worker" \
  --location "Algiers, Algeria" \
  --language fr \
  --qualification "O Level Mathematics and English" \
  --experience "General work" \
  --skill "Computer literacy" \
  --exclusion "Senior management" \
  --profile-confirmed \
  --safety-acknowledged
```

`--cv` accepts either the `cvFileRef` returned by `stage-cv` (the R2 object key, already uploaded) or a local CV path (which `save-profile` uploads to R2). If the user gives many details, use repeated flags. The CV lives in the Agent Genaie Cloudflare R2 bucket under `<uid>/cv/cv.pdf`; Firestore stores that key as `cvFileRef`. Raw WhatsApp media is temporary and must not be treated as the durable CV location.

After saving, reply:

```text
Your Job Scout profile is saved. Make sure Gmail shows Connected in the app. When automatic applications are enabled, I can send email applications and submit straightforward website forms. If a site needs a login, CAPTCHA, payment, declaration, or an answer only you can provide, I'll send you the link instead.
```

## Verify Ready Users

```bash
/home/joseph/.openclaw/workspace/bin/job-scout status --phone "<whatsapp phone>"
```

This returns only the requesting user's sanitized setup state. A user is dispatch-ready only when the WhatsApp chat is linked, Gmail is connected, target roles and a validated location are saved, the profile summary and safety warning are acknowledged, and the CV object is available in R2. The CV is fetched from R2 on demand at dispatch time. Files under `runtime/job-scout/users` are temporary download caches and must never be used to decide whether a CV exists.

The daily dispatcher searches JSearch over a rolling 30-day posting window. It rejects jobs with explicit expired flags or parseable deadlines before today, keeps deadline-free jobs only while they remain in an active source feed, and suppresses jobs already recorded as applied, skipped, action needed, or physically submitted. Candidate artifacts include operator-only per-source diagnostics; do not expose those diagnostics in public chat.

## Admin Manual Dispatch

This section is for Joe's direct admin chat only. It must not be used for public-user self-service.

When Joe asks for a manual run after checking one person's status, scope the run to that person's WhatsApp number:

```bash
/home/joseph/.openclaw/workspace/bin/job-scout dispatch-jsearch \
  --phone "<whatsapp phone>" \
  --run-id "manual-job-scout-<UTC timestamp>"
```

Start the command once, then poll that same process for up to 20 minutes. Several minutes without stdout is normal. Do not launch a second dispatch, do not start a dry run while the first run is active, and do not kill a run only because it is quiet. `--pilot-only` is obsolete and must not be used. If the tool session disappears, verify the named dispatch marker and application records before deciding whether a retry is safe.

## Failure Handling

If setup or saving fails, do not show raw output. Reply:

```text
I couldn't save your Job Scout setup. Try again later.
```

If Gmail is not connected, send:

```text
Connect Gmail in the app first. I'll use it only for applications you approve.
```
