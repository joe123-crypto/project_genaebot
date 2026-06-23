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

Keep public replies short and WhatsApp-friendly.

## Onboarding Flow

```text
User asks for Job Scout
Agent checks whether this WhatsApp chat is linked to an Agent Genaie login
If not linked, agent creates an account-link URL for the requester's WhatsApp phone
User signs in to Agent Genaie with their email
Setup link binds this WhatsApp chat to that app user
User connects Gmail in the app
Agent asks whether the user wants guided questions or CV autofill
Agent collects target roles, target locations, and other details gradually, one or two questions per message
Agent summarizes the profile and asks for corrections
Agent sends scam warning and requires acknowledgement
Agent saves profile through job-scout helper
Daily dispatcher finds ready users and applies for matching jobs
```

## Start Setup

Use the requester's WhatsApp phone number only.

First check whether this WhatsApp chat is already linked to an Agent Genaie login:

```bash
openclaw-cap account-link status --phone "<whatsapp phone>"
```

If the status says `"linked": false`, create an account-link invite for Job Scout:

```bash
openclaw-cap account-link create-invite \
  --phone "<whatsapp phone>" \
  --purpose jobs \
  --next-path /connect-gmail
```

Send the returned `setupUrl` to the user:

```text
Job Scout needs to link this WhatsApp chat to your app login first.

Open this link and sign in with your email:
<setupUrl>

If the sign-in email does not arrive, check your Spam or Junk folder.
```

Stop here until the user has opened the link and signed in. Do not collect CV/profile details before this WhatsApp chat is linked.

If the status says `"linked": true`, create or reuse the Job Scout setup path:

```bash
openclaw-cap job-scout create-invite --phone "<whatsapp phone>"
```

Send the returned `setupUrl` to the user with this public text:

```text
Job Scout is active.

Open this setup link and sign in with your email:
<setupUrl>

After sign-in, the app will open Gmail connection automatically.

Then choose one setup option here:
1. Answer a few short questions.
2. Upload your CV and allow me to pull the details from it.

I will ask only one or two questions at a time.
```

## Setup Option Choice

If the user has not chosen a setup path, ask:

```text
How would you like to set up Job Scout?

1. I ask you a few short questions.
2. You upload your CV and allow me to pull the details from it.
```

Do not continue to CV extraction unless the user clearly chooses option 2 and gives permission.

## Guided Question Flow

Ask one or two questions per message, then wait for the user to answer before asking the next question.

Recommended order:

1. What kind of jobs should I look for?
2. Which city, area, or country should I target?
3. What qualifications or certificates do you have?
4. What work experience should I mention?
5. What skills should I mention in applications?
6. Are there any jobs you do not want?
7. Is there anything else I should remember when applying?
8. Please upload your CV.

If the user already answered some of these, do not ask again. Ask only for missing or unclear details.

Friendly field mapping:

- Jobs to look for -> `--target-role` (repeat for multiple roles)
- Preferred city, area, or country -> `--location` (repeat for multiple locations)
- Search country -> `--country` (default `dz` for Algeria)
- Search language -> `--language` (default `fr` for French)
- Qualifications or certificates -> `--qualification`
- Work experience -> `--experience`
- Useful skills -> `--skill`
- Jobs to avoid -> `--exclusion`
- Anything else to remember -> `--prompt-note`
- Uploaded CV -> `--cv`

When the user uploads the CV, immediately stage it into durable Job Scout storage:

```bash
openclaw-cap job-scout stage-cv \
  --phone "<whatsapp phone>" \
  --cv "<MediaPath>"
```

Use the returned `cvFileRef` for later extraction and saving. Do not mention internal file paths to the user.

## CV Autofill Flow

If the user chooses CV autofill, ask for explicit permission before reading/extracting from the CV:

```text
Please upload your CV and confirm:
"Yes, use my CV to set up Job Scout."
```

After the user uploads the CV and gives permission:

- Immediately stage the CV into durable Job Scout storage:

```bash
openclaw-cap job-scout stage-cv \
  --phone "<whatsapp phone>" \
  --cv "<MediaPath>"
```

- Use the returned `cvFileRef` for CV extraction and final profile saving.
- Extract likely job types, qualifications, work experience, and skills from the CV.
- Ask follow-up questions only for missing or unclear details.
- Ask at most one or two questions per message.
- Do not invent details not supported by the CV or the user's answers.

Before saving, summarize what was understood:

```text
Here is what I understood:

Jobs to look for: ...
Preferred location: ...
Qualifications: ...
Experience: ...
Skills: ...
Jobs to avoid: ...
Notes for applications: ...

Is this correct, or should I change anything?
```

Save only after the user confirms or corrects the summary.

## Scam Warning Consent

Before saving the final Job Scout profile, send this warning:

```text
Before I continue, please note:

Real employers should not ask you to pay money upfront for an application, interview, medical check, training slot, uniform, or job placement. If anyone asks you to pay first, treat it as suspicious.

This service helps search and apply for jobs, but it cannot guarantee that every employer or listing is legitimate. If you are scammed by a third party, Genaie is not responsible for that loss.

Please reply: "I understand" to continue.
```

Rules:

- Do not save the profile until the user clearly acknowledges the warning.
- Accept clear acknowledgements such as "I understand", "I agree", "understood", or equivalent.
- If the user refuses, stop onboarding politely and do not save the profile.
- If the user is unsure, explain briefly that they should never pay money upfront, then ask whether they understand and want to continue.

## Save Profile

After the user has provided enough details, uploaded a CV, confirmed the summary, and acknowledged the scam warning, save the profile:

```bash
openclaw-cap job-scout save-profile \
  --phone "<whatsapp phone>" \
  --cv "<cvFileRef returned by stage-cv>" \
  --target-role "General worker" \
  --location "Algiers" \
  --country dz \
  --language fr \
  --qualification "O Level Mathematics and English" \
  --experience "General work" \
  --skill "Computer literacy" \
  --exclusion "Senior management"
```

If the user gives many details, use repeated flags. The helper keeps the CV in the per-user Job Scout folder and stores only the file reference in Agent Genaie/Firestore. Raw WhatsApp media is temporary and must not be treated as the durable CV location.

After saving, reply:

```text
Your Job Scout profile is saved.

Make sure Gmail shows Connected in the app. Once it is connected, the scout can apply for matching jobs and send updates here.
```

## Verify Ready Users

```bash
openclaw-cap job-scout list-ready
```

This lists only users who have an active Job Scout profile, linked WhatsApp chat, CV reference, and connected Gmail sender.

## Failure Handling

If setup or saving fails, do not show raw output. Reply:

```text
I couldn't save your Job Scout setup right now. Please try again later.
```

If Gmail is not connected, send:

```text
Please connect Gmail in the app first. This lets applications be sent from your own email after you approve access.
```
