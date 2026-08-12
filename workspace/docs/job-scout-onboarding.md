# Job Scout Onboarding

Purpose: onboard a public WhatsApp user for Job Scout without exposing internal implementation details or asking for private Gmail credentials in chat.

## Public Boundary

Allowed in public chat:

- Ask whether the user wants web or WhatsApp setup before sending any URL.
- Send the canonical Agent Genaie WhatsApp-link URL only after that choice.
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
User signs in to Agent Genaie; Gmail is granted as part of sign-in (single consent)
First-time linking is automatic: the WhatsApp number is linked without a confirmation step
Sign-in redirects the user back to this WhatsApp chat automatically
If user chose web setup, normal web onboarding continues with Job Scout already selected
If user chose chat setup and already has a CV in R2, Agent uses CV autofill automatically
If user chose chat setup and has no CV in R2, Agent asks whether to answer questions or upload a CV
Always confirm the preferred location and country; a CV cannot reliably supply it
Agent collects any remaining details gradually, one or two questions per message
Agent summarizes the profile and asks for corrections
Agent validates every preferred location before saving
Agent sends scam warning and requires acknowledgement
Agent saves profile through job-scout helper
Daily dispatcher finds and scores matching jobs for ready users
If autoApply is enabled, Job Scout customizes and submits strong matches
If autoApply is disabled, Job Scout records strong email and website matches as waiting for approval
After the user approves a match in the dashboard, the approval worker customizes and submits it
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

For an unlinked user, the returned URL must use `/whatsapp?token=...`. After sign-in it becomes `/<publicUserId>/whatsapp?token=...`. For a first-time signup the WhatsApp number is linked automatically and the user is redirected back to the WhatsApp chat without any confirmation step. A returning user who is changing their linked number must confirm the change explicitly before it takes effect.

Do not send or recreate `/account-link/setup`, `/job-scout/setup`, or either legacy confirm URL.

## Setup Option Choice

If the user has not chosen web setup or WhatsApp setup, ask:

```text
How do you want to set up Job Scout?

1. In the web app
2. Here in WhatsApp
```

If the user chooses option 1, send the returned canonical or resume URL and stop. Agent Genaie continues the normal web onboarding flow with Job Scout preselected.

```text
Finish setting up Job Scout in the web app:
<setupUrl or nextUrl>
```

If the user chooses option 2, send the returned canonical link. Sign-in grants Gmail access as part of the same step, and the app redirects the user back to WhatsApp automatically. Once the user returns, check `api.cv_object_available()`. If a CV already exists in R2, go straight to the CV Autofill Flow. Otherwise ask:

```text
How do you want to set up Job Scout in chat?

1. Answer a few questions.
2. Upload your CV.
```

Do not continue to CV extraction unless the user chose WhatsApp setup and gives permission. Saving a ready WhatsApp-collected profile completes the in-progress Job Scout onboarding automatically.

## Guided Question Flow

Use this flow only when the user does not already have a CV in R2. Check with `api.cv_object_available()` before asking questions. If a CV exists, use the CV Autofill Flow instead.

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

Use this flow when the user already has a CV in R2 (`api.cv_object_available()` returns true) or when the user explicitly chooses CV autofill.

If a CV already exists in R2, use the stored `cvFileRef` directly. Tell the user their existing CV was found and ask permission to extract from it:

```text
I found your CV on file. Can I use it to prefill your Job Scout profile?
```

If the user is uploading a new CV, ask for explicit permission first:

```text
Upload your CV and say, "Use my CV for Job Scout."
```

After the user grants permission and the CV source is confirmed:

- If a new CV was uploaded, stage it first and use the returned `cvFileRef` (R2 object key `<uid>/cv/cv.pdf`) for CV extraction and final profile saving:

```bash
/home/joseph/.openclaw/workspace/bin/job-scout stage-cv \
  --phone "<whatsapp phone>" \
  --cv "<MediaPath>"
```

- If the user's existing CV was already in R2, no `stage-cv` call is needed. The existing key is retained automatically when you omit `--cv` from `save-profile`.
- Extract likely job types, qualifications, work experience, and skills from the CV.
- Ask follow-up questions only for missing or unclear details.
- Always confirm the preferred location and country. A CV cannot reliably supply a current location. Ask: "Which city or area should I search for jobs? Include the country, for example: Bindura, Zimbabwe."
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

After the user has provided enough details, confirmed the summary, and acknowledged the scam warning, save the profile:

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

`--cv` accepts either the `cvFileRef` returned by `stage-cv` (the R2 object key, already uploaded) or a local CV path (which `save-profile` uploads to R2). When the user already has a CV in R2 and did not upload a new one during this session, `--cv` may be omitted; `save-profile` will retain the existing CV on file. If the user gives many details, use repeated flags. The CV lives in the Agent Genaie Cloudflare R2 bucket under `<uid>/cv/cv.pdf`; Firestore stores that key as `cvFileRef`. Raw WhatsApp media is temporary and must not be treated as the durable CV location.

After saving, reply:

```text
Your Job Scout profile is saved. When automatic applications are enabled, I can send email applications and submit straightforward website forms. If a site needs a login, CAPTCHA, payment, declaration, or an answer only you can provide, I'll send you the link instead.
```

When automatic applications are disabled, explain that strong matches will
wait in the dashboard and that CV customization and submission happen only
after the user approves each one.

## Verify Ready Users

```bash
/home/joseph/.openclaw/workspace/bin/job-scout status --phone "<whatsapp phone>"
```

This returns only the requesting user's sanitized setup state. A WhatsApp
onboarding user is dispatch-ready only when the chat is linked, Gmail is
connected, target roles and a validated location are saved, the profile summary
and safety warning are acknowledged, and the CV object is available in R2. Gmail
is connected as part of Google sign-in; it does not require a separate step for
users who sign in with Google. A user who completed setup in the web app can be
dispatch-ready without a linked WhatsApp phone when the same Gmail, profile,
safety, subscription, and CV requirements are satisfied. A web-only user receives no WhatsApp summary.
When that user's `autoApply` setting is false, Job Scout records both email and
website matches as `pending_approval` without personalizing a CV or submitting
anything. The CV is fetched from R2 on demand when an automatic application or
an approved application needs customization. Files under
`runtime/job-scout/users` are temporary download caches and must never be used
to decide whether a CV exists.

The daily 08:00 `Africa/Harare` cron invokes the trusted
`job-scout-agent --daily-live` command wrapper. The wrapper prepares one durable
run before opening an isolated OpenClaw `main` parent turn, waits on durable
claim state instead of child announcement delivery, and verifies the finished
run before it can exit successfully. It searches JSearch over a rolling 30-day
posting window, rejects jobs with explicit expired flags or parseable deadlines
before today, keeps deadline-free jobs only while they remain in an active
source feed, and suppresses jobs already recorded as applied, skipped, action
needed, pending approval, or physically submitted. The parent processes every
ready user through at most four isolated children at a time. Each child inherits
the default model, assesses a deterministic shortlist of at most 20 jobs, and
selects the highest scores at or above 51 without a hard mismatch, up to the
user's configured per-run application limit. For `autoApply: false`, completion
records selected email and website
matches as `pending_approval` and creates no application documents. For
`autoApply: true`, the child personalizes truthful application documents before
submission. Each claim creates a private result template with every opaque
candidate ID populated deterministically; the child edits only numbered
assessment values and personalized artifact paths, and the dispatcher maps each
short number back to the canonical stored ID. The dispatcher validates the
child packet and remains the only process allowed to send or submit. A child
failure records a fail-closed outcome and sends no template documents. Strict
final verification makes the cron fail when any subscriber outcome fails or
when a requested WhatsApp summary fails, which activates the configured admin
failure alert. Candidate artifacts and agent diagnostics are operator-only; do
not expose them in public chat.

The separate `job-scout-approvals` cron runs every five minutes. It reconstructs
each dashboard-approved vacancy from the immutable `candidateSnapshot`, asks an
isolated child to customize the CV and application letter, and lets the
dispatcher send the email application or submit a supported website form. A
dashboard rejection remains skipped and is never submitted. Sites that require
a login, CAPTCHA, payment, declaration, missing user answer, or unsupported
manual step become action required instead of being submitted unsafely.

## Admin Manual Dispatch

This section is for Joe's direct admin chat only. It must not be used for public-user self-service.

When Joe asks for a manual run after checking one person's status, scope the agent run to that person's WhatsApp number:

```bash
/home/joseph/.openclaw/workspace/bin/job-scout-agent \
  --phone "<whatsapp phone>" \
  --live
```

For a web-only subscriber without a linked WhatsApp number, scope the run to the
Firebase user ID instead:

```bash
/home/joseph/.openclaw/workspace/bin/job-scout-agent \
  --user-id "<firebase uid>" \
  --live
```

The UID is an operator-only identifier and must never be requested from or
shown to a public user. A UID-scoped web-only run follows the subscriber's
`autoApply` policy, includes website matches in approval-first discovery, and
intentionally sends no WhatsApp summary.

Use `--preview` instead of `--live` to exercise the same model workflow without external side effects. The manual wrapper now completes quiet job discovery before it opens the model turn, so the model begins from durable status and cannot time out or repeat preparation. Start the command once with a 21600-second outer timeout and poll that same process for up to six hours. Several minutes without stdout is normal. Do not launch a second dispatch and do not kill a run only because it is quiet. `--pilot-only` and direct operator dispatches are obsolete. If the tool session disappears, verify the named dispatch marker and application records before deciding whether a retry is safe.

The first recorded outcome for each claim is immutable. A child must never edit a
claim, work packet, dispatch marker, status, or outcome, and must never retry
`claim` or `complete`. The wrapper remains attached while the parent waits on
durable claim state and exits only after the dispatch marker passes strict
verification.

## Failure Handling

If setup or saving fails, do not show raw output. Reply:

```text
I couldn't save your Job Scout setup. Try again later.
```

If Gmail is not connected (this can happen when the user signed in via an email link rather than Google sign-in), send:

```text
Connect Gmail in the app first. I'll use it only for applications you approve.
```
