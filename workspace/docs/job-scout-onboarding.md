# Job Scout Onboarding

Purpose: onboard a public WhatsApp user for Job Scout without exposing internal implementation details or asking for private Gmail credentials in chat.

## Public Boundary

Allowed in public chat:

- Ask whether the user wants web or WhatsApp setup before sending any URL.
- Send the canonical Agent Genaie WhatsApp-link URL only after that choice.
- Tell the user to sign in and connect Gmail in the app.
- Offer two setup paths: web setup or chat setup.
- If the user chooses chat setup, offer guided questions or CV autofill.
- Ask only one or two onboarding questions per message.
- Ask the user to upload their CV in this WhatsApp chat only after they choose chat setup.
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
Agent asks whether setup should continue in the web app or in WhatsApp
Agent records that choice and starts or resumes Job Scout for the requester
If the chat is not linked, Agent Genaie creates the canonical WhatsApp-link URL
User signs in to Agent Genaie with their email
The scoped WhatsApp page shows the originating number and asks only for confirmation
If user chose web setup, normal web onboarding continues with Job Scout already selected
If user chose chat setup, user connects Gmail in the app and returns to WhatsApp
Agent asks whether the user wants guided questions or CV autofill inside chat
Agent collects target roles, a preferred location that includes its country, and other details gradually, one or two questions per message
Agent summarizes the profile and asks for corrections
Agent sends scam warning and requires acknowledgement
Agent saves profile through job-scout helper
Daily dispatcher finds ready users and applies for matching jobs
```

## Start Setup

Use the sender-bound `public_service` action `job_scout_start_or_status` and pass the user's current message as `text`. The tool owns the requester's phone and setup state.

On the first setup request, send only this choice. Do not create or send a link yet:

```text
How do you want to set up Job Scout?

1. In the web app
2. Here in WhatsApp
```

Call the same action again with the user's choice. A current choice replaces an older pending Job Scout choice or link. If the user is already linked, the tool returns the next missing step instead of restarting setup.

For an unlinked user, the returned URL must use `/whatsapp?token=...`. After sign-in it becomes `/<publicUserId>/whatsapp?token=...`. The page shows the masked originating WhatsApp number and a **Confirm and link** action; it must not ask that user to type a number.

Do not send or recreate `/account-link/setup`, `/job-scout/setup`, or either legacy confirm URL.

## Setup Option Choice

If the user has not chosen web setup or WhatsApp setup, ask:

```text
How do you want to set up Job Scout?

1. In the web app
2. Here in WhatsApp
```

If the user chooses option 1, send the returned canonical or resume URL and stop. After confirmation, Agent Genaie continues the normal web onboarding flow with Job Scout preselected. Only service selection is skipped; WhatsApp confirmation, Gmail connection, and the Job Scout profile remain part of onboarding.

```text
Finish setting up Job Scout in the web app:
<setupUrl or nextUrl>
```

If the user chooses option 2, send the returned canonical link or Gmail resume URL. After Gmail is connected, Agent Genaie hands the user back to WhatsApp. Then ask:

```text
How do you want to set up Job Scout in chat?

1. Answer a few questions.
2. Upload your CV.
```

Do not continue to CV extraction unless the user chose WhatsApp setup and gives permission. Saving a ready WhatsApp-collected profile completes the in-progress Job Scout onboarding automatically.

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
- Search language -> `--language`; it controls the JSearch language and query wording (`à` for French, `in` otherwise), with `en` as the default
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

The uploaded CV is only the source of the applicant's facts. At application time
the agent redesigns it into a polished, self-contained, single-page CV tailored
to each job, so a plain or roughly formatted upload is fine; its exact styling
is not sent to employers.

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
Never pay upfront for an application, interview, medical check, training, uniform, or job placement. Scammers exist, and Genaie is not accountable if you get scammed.

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
  --language en \
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

The daily 08:00 `Africa/Harare` run is an isolated OpenClaw `main` agent turn.
It searches JSearch over a rolling 30-day posting window, rejects jobs with
explicit expired flags or parseable deadlines before today, keeps deadline-free
jobs only while they remain in an active source feed, and suppresses jobs
already recorded as applied, skipped, action needed, or physically submitted.
The parent processes every ready user through at most four isolated children at
a time. Each child inherits the default model, assesses a deterministic
shortlist of at most 20 jobs, selects only scores of 70 or higher without a hard
mismatch, and personalizes truthful application documents. Each claim creates a
private result template with every opaque candidate ID populated deterministically;
the child edits only numbered assessment values and personalized artifact paths,
and the dispatcher maps each short number back to the canonical stored ID.
The dispatcher validates the child packet and remains the only process allowed
to send or submit. A child failure records a fail-closed outcome and sends no
template documents. Candidate artifacts and agent diagnostics are operator-only;
do not expose them in public chat.

## Admin Manual Dispatch

This section is for Joe's direct admin chat only. It must not be used for public-user self-service.

When Joe asks for a manual run after checking one person's status, scope the agent run to that person's WhatsApp number:

```bash
/home/joseph/.openclaw/workspace/bin/job-scout-agent \
  --phone "<whatsapp phone>" \
  --live
```

Use `--preview` instead of `--live` to exercise the same model workflow without external side effects. The manual wrapper now completes quiet job discovery before it opens the model turn, so the model begins from durable status and cannot time out or repeat preparation. Start the command once with a 21600-second outer timeout and poll that same process for up to six hours. Several minutes without stdout is normal. Do not launch a second dispatch and do not kill a run only because it is quiet. `--pilot-only` and direct operator dispatches are obsolete. If the tool session disappears, verify the named dispatch marker and application records before deciding whether a retry is safe.

The first recorded outcome for each claim is immutable. A child must never edit a
claim, work packet, dispatch marker, status, or outcome, and must never retry
`claim` or `complete`. The wrapper remains attached after the parent yields and
exits only after the durable dispatch marker passes verification.

## Failure Handling

If setup or saving fails, do not show raw output. Reply:

```text
I couldn't save your Job Scout setup. Try again later.
```

If Gmail is not connected, send:

```text
Connect Gmail in the app first. I'll use it only for applications you approve.
```
