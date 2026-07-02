---
name: job-scout
description: Onboard public WhatsApp users for Agent Genaie Job Scout and manage Job Scout records.
---

# job-scout

Use this skill for public Job Scout setup and daily job application dispatch.

Commands:

- Check app account link first: `/home/joseph/.openclaw/workspace/bin/account-link status --phone "<whatsapp phone>"`
- If not linked, create login link: `/home/joseph/.openclaw/workspace/bin/account-link create-invite --phone "<whatsapp phone>" --purpose jobs --next-path /connect-gmail`
- After linked, create Job Scout setup link: `/home/joseph/.openclaw/workspace/bin/job-scout create-invite --phone "<whatsapp phone>"`
- Validate the preferred location before saving: `/home/joseph/.openclaw/workspace/bin/job-scout resolve-location --location "Bindura, Zimbabwe"`
- Save profile and CV: `/home/joseph/.openclaw/workspace/bin/job-scout save-profile --phone "<whatsapp phone>" --cv "/path/to/cv.pdf" --target-role "..." --location "City or area, Country" --language fr`
- Check one requester's setup: `/home/joseph/.openclaw/workspace/bin/job-scout status --phone "<whatsapp phone>"`
- List applications: `/home/joseph/.openclaw/workspace/bin/job-scout list-applications --user-id "<firebase uid>"`
- Record application: `/home/joseph/.openclaw/workspace/bin/job-scout record-application --user-id "<firebase uid>" --company "..." --role "..." --status applied`
- Fetch JSearch candidates for a profile: `/home/joseph/.openclaw/workspace/bin/job-scout fetch-candidates --target-role "..." --location "..."`
- Preview candidates for ready users: `/home/joseph/.openclaw/workspace/bin/job-scout dispatch-jsearch --dry-run`

Rules:

- Use only the requester's WhatsApp phone number.
- Link the WhatsApp chat to Agent Genaie before collecting CV/profile details.
- Ask where jobs should be scouted and explicitly require `City or area, Country`, for example `Bindura, Zimbabwe`. Validate it with `resolve-location`; if validation fails, ask the user to restate it instead of guessing.
- Never ask for Gmail passwords, OTPs, OAuth codes, tokens, API keys, or Firebase UIDs in public chat.
- Do not expose raw helper output, Firestore paths, internal API keys, logs, stack traces, or other users' data to public users.
- Real application email sends go through `genaie-mail --from <connected sender>`.
- Website applications go only through the isolated browser worker. Report `applied` only after its `submitted` result; login, CAPTCHA, declarations, payments, and unknown required questions are `action_required`.
- Never claim that an `action_required`, historical `physical_submission`, or failed record was submitted.
- Use `status --phone` when answering whether a requester is set up. Cloud R2 is authoritative for CV availability; never infer it from files under `runtime/job-scout/users`.
- `list-ready` and full dispatch commands are operator-only because their output can include multiple users. Never use them to answer a public user's status question.
- Job discovery must use the deterministic JSearch dispatcher only. Each saved preferred location supplies that user's JSearch country code. Algeria (`dz`) remains the fallback when no location is supplied outside onboarding; default language is French (`fr`).
