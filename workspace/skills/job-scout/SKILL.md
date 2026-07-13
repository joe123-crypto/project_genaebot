---
name: job-scout
description: Onboard public WhatsApp users for Agent Genaie Job Scout and manage Job Scout records.
---

# job-scout

Use this skill for public Job Scout setup and daily job application dispatch.

Commands:

- Ask whether the user wants web or WhatsApp setup before creating any link.
- Start or resume the selected route: `/home/joseph/.openclaw/workspace/bin/account-link start-onboarding --phone "<whatsapp phone>" --service jobs --channel <web|chat>`
- Send the returned canonical `/whatsapp?token=...` URL or `nextUrl`. Never use `/account-link/setup` or `/job-scout/setup`.
- If the user chooses WhatsApp setup, connect Gmail in the app, return to chat, and continue with the chat commands below.
- Validate the preferred location before saving: `/home/joseph/.openclaw/workspace/bin/job-scout resolve-location --location "Bindura, Zimbabwe"`
- Save a confirmed profile and CV after the safety acknowledgement: `/home/joseph/.openclaw/workspace/bin/job-scout save-profile --phone "<whatsapp phone>" --cv "/path/to/cv.pdf" --target-role "..." --location "City or area, Country" --language en --profile-confirmed --safety-acknowledged`
- Check one requester's setup: `/home/joseph/.openclaw/workspace/bin/job-scout status --phone "<whatsapp phone>"`
- List applications: `/home/joseph/.openclaw/workspace/bin/job-scout list-applications --user-id "<firebase uid>"`
- Record application: `/home/joseph/.openclaw/workspace/bin/job-scout record-application --user-id "<firebase uid>" --company "..." --role "..." --status applied`
- Fetch JSearch candidates for a profile: `/home/joseph/.openclaw/workspace/bin/job-scout fetch-candidates --target-role "..." --location "..."`
- Preview candidates for ready users: `/home/joseph/.openclaw/workspace/bin/job-scout dispatch-jsearch --dry-run`

Admin manual runs:

- A request made after checking one person is scoped to that person. Run `/home/joseph/.openclaw/workspace/bin/job-scout dispatch-jsearch --phone "<whatsapp phone>" --run-id "manual-job-scout-<UTC timestamp>"` exactly once.
- A running process with no new output is not a failure. Job discovery and website checks normally take several minutes. Poll the same process for up to 20 minutes.
- Never start a direct or dry run while another dispatch is running. The command lock will reject overlaps.
- Never use `--pilot-only`; it is obsolete. Do not infer current arguments from historical run diagnostics.
- If the process connection is lost, inspect the named dispatch marker and application records before deciding whether another run is safe.

Rules:

- Use only the requester's WhatsApp phone number.
- Ask web or WhatsApp before creating the account-link URL, then link the chat before collecting CV/profile details.
- Web setup continues normal Agent Genaie onboarding with Job Scout preselected and does not collect CV/profile details in chat.
- Chat setup keeps the existing guided questions, CV staging, profile confirmation, safety acknowledgement, and save-profile flow.
- Ask where jobs should be scouted and explicitly require `City or area, Country`, for example `Bindura, Zimbabwe`. Validate it with `resolve-location`; if validation fails, ask the user to restate it instead of guessing.
- Never ask for Gmail passwords, OTPs, OAuth codes, tokens, API keys, or Firebase UIDs in public chat.
- Do not expose raw helper output, Firestore paths, internal API keys, logs, stack traces, or other users' data to public users.
- Real application email sends go through `genaie-mail --from <connected sender>`.
- Website applications go only through the isolated browser worker. Report `applied` only after its `submitted` result; login, CAPTCHA, declarations, payments, and unknown required questions are `action_required`.
- Never claim that an `action_required`, historical `physical_submission`, or failed record was submitted.
- Use `status --phone` when answering whether a requester is set up. Cloud R2 is authoritative for CV availability; never infer it from files under `runtime/job-scout/users`.
- `save-profile` requires both `--profile-confirmed` and `--safety-acknowledged`. Never pass either flag until the requester has explicitly completed that step.
- `list-ready` and full dispatch commands are operator-only because their output can include multiple users. Never use them to answer a public user's status question.
- Job discovery must use the deterministic JSearch dispatcher only. Each saved preferred location supplies that user's JSearch country code. Algeria (`dz`) remains the fallback when no location is supplied outside onboarding; default language is English (`en`).
