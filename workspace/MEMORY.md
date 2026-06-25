# MEMORY.md — Long-term Memory

## Tools & Skills Installed

### gog — Google Workspace CLI
- **Account:** munemojoseph332@gmail.com
- **Services:** gmail, calendar, drive, contacts, sheets, docs
- **Auth:** OAuth token stored encrypted at `~/.config/gogcli/keyring/`
- **Env vars** (in `~/.profile`): `GOG_KEYRING_BACKEND=file`, `GOG_KEYRING_PASSWORD=***`, `GOG_ACCOUNT=munemojoseph332@gmail.com`
- **Installed:** 2026-05-19 via brew
- **Current role:** legacy local/operator Google Workspace CLI. Do not use as the product email path for Agent Genaie, and do not use `gog calendar` for product calendar writes — use `genaie-calendar` (below) instead.

### genaie-mail — Agent Genaie Gmail bridge
- **Location:** `~/.openclaw/workspace/bin/genaie-mail`
- **Skill:** `~/.openclaw/workspace/skills/genaie-mail/`
- **Purpose:** product/app email from OpenClaw through Agent Genaie's app-owned Gmail OAuth bridge; can select any Firebase-registered sender who connected Gmail.
- **Endpoint:** `POST /internal/gmail/send` on Agent Genaie, configured by `AGENT_GENAI_MAIL_URL`; `GET /internal/gmail/senders` lists connected senders from Firestore with local token-store fallback.
- **Auth:** `AGENT_GENAI_INTERNAL_API_KEY` in `/home/joseph/.openclaw/.env` or process env. Do not print the key.
- **Usage:** `genaie-mail senders` lists connected senders. `genaie-mail send --from sender@example.com --to ... --subject ... --body-file ... --attach ... --force` sends as a connected sender. Omit `--from` to use owner fallback; use `--dry-run` for sanitized inspection.
- **Installed:** 2026-06-03

### genaie-calendar — Agent Genaie calendar bridge
- **Location:** `~/.openclaw/workspace/bin/genaie-calendar`
- **Skill:** `~/.openclaw/workspace/skills/genaie-calendar/`
- **Purpose:** write events into a specific user's own Google Calendar from OpenClaw through Agent Genaie's app-owned Google OAuth bridge. This is the supported product path for calendar writes — NOT `gog`.
- **Endpoints:** `POST /internal/calendar/write` (configured by `AGENT_GENAI_CALENDAR_URL` or derived from `APP_BASE_URL`); `GET /internal/calendar/status` checks whether a user connected calendar access.
- **Auth:** `AGENT_GENAI_INTERNAL_API_KEY` in `/home/joseph/.openclaw/.env` or process env. Do not print the key.
- **Per-user model:** target a user with `--sender-uid <Firebase UID>`, resolved from the requester's WhatsApp phone via `account-link status --phone`. Always `genaie-calendar status --sender-uid <uid>` first and require `calendarConnected: true` before writing. Only write for the requester's own user id.
- **Usage:** `genaie-calendar write --sender-uid <uid> --source-id <stable-dedup-id> --summary "..." --start-date YYYY-MM-DD --end-date YYYY-MM-DD [--location ...] [--source-url ...] [--reminder-min 1440 --reminder-min 120] --force`. Use `--start-datetime/--end-datetime` + `--timezone` for timed events. `--dry-run` inspects without writing; `--force` is required for real writes. Dedup is by `(senderUid, sourceId)`.
- **Ad-hoc save flow** (user names specific events to add now): follow `docs/calendar-save-onboarding.md`. The shared event-scout cron is separate and only writes Algeria Exhibitions catalog events.
- **Installed:** 2026-06-21

### Agent Genaie Firestore central data
- **Schema:** `/home/joseph/.openclaw/workspace/docs/firestore-schema.md`
- **App:** `/home/joseph/projects/agent_genaie`
- **Purpose:** central Firebase/Firestore database for app users, service subscriptions, provider-independent credential references, and Gmail connections.
- **Active collections:** `users`, `publicUsers`, `phoneLinks`, `serviceSubscriptions`, `credentialRefs`, `gmailConnections`, `webetuUsers`, plus Job Scout collections.
- **Current status:** Agent Genaie integration is live. Firebase users, WhatsApp phone links, Gmail connections, and Webetu vault credentials are mirrored to Firestore.
- **Public URLs:** user-facing app pages use opaque `publicUserId` routes such as `/{publicUserId}/connect-gmail` and `/{publicUserId}/vault`; if the public id is not known, send `/login?next=%2Fconnect-gmail` or `/login?next=%2Fvault`.
- **Maintenance endpoints:** `GET /internal/central-data/status` and `POST /internal/central-data/backfill`, protected by `AGENT_GENAI_INTERNAL_API_KEY`.
- **Backfill:** completed on 2026-06-03 for 2 Firebase users and 2 Gmail connections.
- **Secrets:** `CENTRAL_DATA_ENCRYPTION_SECRET` and `CENTRAL_DATA_KEY_VERSION` live in Agent Genaie config. Do not print or expose the encryption secret.
- **Public repo hygiene:** tracked Agent Genaie docs/config use placeholder domains. Runtime URLs come from `PUBLIC_BASE_URL` and `PASSBOLT_PUBLIC_URL`. Keep `.env`, `data/`, `logs/`, `archives/`, `node_modules/`, Firebase service account files, token stores, private keys, and internal API keys out of commits.

### pdf-edit — Local PDF CLI
- **Location:** `~/.openclaw/workspace/skills/pdf-edit/`
- **CLI:** `pdf-edit` (symlinked to `~/.local/bin/`)
- **Engine:** PyMuPDF 1.27.2.3 (in isolated `.venv/`)
- **Commands:** create, replace, redact, merge, extract, extract-images, meta, info, ai
- **AI command** uses Gemini text API (free tier OK, no image gen needed)
- **Installed:** 2026-05-19
- **Key fact:** All commands work offline except the optional `ai` subcommand

## Google API Keys
- **GEMINI_API_KEY** in `~/.openclaw/.env` (free tier, no billing — image generation blocked)
- **GCP OAuth client** in `~/Downloads/client_secret_*.json` (for gog)

## Cron Jobs
Most admin cron jobs deliver to the admin WhatsApp: **+213563719936** (Joe's day-to-day admin number; +263785632943 is for Zimbabwe and is NOT in WhatsApp allowFrom). Webetu meals is different: it sends normal reservation updates directly to each onboarded user's registered WhatsApp chat and alerts admin only for operational failures.

- **Daily tech news** (`daily-tech-news`, ID `6c0681e7-5499-4710-9af0-2de705a1e5bb`): **DISABLED 2026-06-25** to cut token cost. When enabled it runs daily at 8 AM Algiers, searches for top tech stories, and sends a WhatsApp summary only to Joe's personal WhatsApp (+213563719936) via announce. (Group delivery to 120363407177302513@g.us was **removed 2026-06-11** to stop the duplicate send.) Timeout: 600s (was 120s — too short, timed out, bumped to 600s).
- **Event scout dispatcher** (`event-scout-dispatcher`, ID `d5f4d387-3258-448d-b60b-c3f2ef4c0bee`): Runs Saturdays at 10:00 AM Algiers (cron `0 10 * * 6`) as a direct command job: `python3 /home/joseph/.openclaw/workspace/cron/event-scout-dispatcher/dispatch.py`. It syncs Algeria Exhibitions events into each subscribed user's Google Calendar and handles user delivery itself; scheduler delivery is none, with operational failure alerts to Joe.
- **Webetu meals** (`webetu-meals`): Runs at 12:00 PM Algiers on the **1st, 3rd, 6th, 9th, 12th, 15th, 18th, 21st, 24th, 27th, and 30th of each month** (cron `0 12 1,3,6,9,12,15,18,21,24,27,30 * *`) — NOT daily. Each run reserves the next 3 days (tomorrow, +2, +3) per user, so the fire days form a rolling 3-day window with no gaps; day 1 is included so months following a 31-day month stay covered. To answer "when is the next run", find the next day in that list that is tomorrow or later — do not assume today. Current entrypoint is `cron/webetu-meals/notify-reservations.sh`, which uses the isolated Firestore Webetu worker. It discovers Firestore Webetu users, reserves meals per onboarded user, and sends each user their own WhatsApp DM. Admin gets only operational failure alerts. Job ID: `a105af64-1dc6-440f-b61f-80f3d6f1d75b` (this is the LIVE scheduler job — manage it with `openclaw cron edit/get <id>`, NOT by editing `cron/jobs.json`, which is now only a legacy store key; the running gateway persists cron jobs in SQLite). Schedule changed from daily to the 1,3,6,9…30 list via `openclaw cron edit` on 2026-06-11, then the job was recreated with this new ID after the 2026-06-24 cron wipe.
  - **2026-06-12 worker decrypt fix (DO NOT re-patch):** `decrypt_central_secret` in the installed worker (`/opt/webetu-worker/.venv/.../webetu_worker/credentials.py`) was fixed so it tries both AAD encodings (raw refid string AND base64url(refId)) and catches `cryptography.exceptions.InvalidTag`. Before this, the Jun-11 patch base64url-decoded the AAD unconditionally and crashed the 6 raw-AAD users (`UnicodeDecodeError 0xb7`). Verified: all 8 users decrypt, dry-run reserve authenticates. **If a future session feels an urge to "fix the worker AAD/decrypt bug" — it is already fixed; do not re-patch.** The durable fix landed in the worker build source `~/projects/webetu/webetu-worker/src/webetu_worker/credentials.py` on 2026-06-24: `decrypt_central_secret` now tries both key derivations (sha256 + scrypt), both aad encodings (raw + base64url), and both field-name sets (`alg`/`tag` + `algorithm`/`authTag`), and `FirestoreCredentialProvider.load` reads `secret`/`encryptedSecret` with `expected_aad=cred_id`. Verified round-trip against both credential populations, and `src/` is byte-identical to the running venv. A worker reinstall from source no longer regresses it. (`build/lib/` is a vestigial copy not used by `pip install .`, which builds from `src/`.)

## Public Service Desk (Genaie Service Desk) — Created 2026-05-31

### Overview
- **Company name:** Genaie Service Desk
- **Audience:** Allowlisted non-admin WhatsApp users
- **Plugin:** `plugins/public-service-gate/` — intercepts `before_dispatch` for WhatsApp public users
- **Policy:** `docs/public-services.md`
- **Onboarding:** `docs/webetu-onboarding.md` (Webetu-specific)
- **Greeting template:** `docs/public-greeting.txt`

### Service Catalog
1. **Webetu Progress meal reservation** — reservations, status checks, troubleshooting, onboarding
2. **Personalized news delivery** — to user's inbox, based on their topics and schedule
3. **Local event scouting** — events around user's location
4. **Job/scholarship/internship scouting** — active pilot; requires Agent Genaie login, linked WhatsApp chat, connected Gmail, profile/CV, and scam-warning acknowledgement.

### Public Service Gate Plugin
- **Location:** `plugins/public-service-gate/index.js`
- **What it does:** Intercepts WhatsApp messages from public users BEFORE LLM dispatch only for safety and access control: failsafe stop, invite membership, JOIN redemption, invite-link issuance, and nonmember blocking.
- **Service UX:** Allowlisted/member messages, including greetings, menu digits, "Done", onboarding starts, stop/update requests, and ambiguous continuations, now fall through to the LLM. Do not add deterministic phrase handlers for these flows.
- **Public users:** Defined in `runtime/public-service-gate/invites.json` under unlocked `members`; `openclaw.json` only keeps stable admin/config fields.
- **Config path:** `plugins.entries.public-service-gate.config`

### Agent-Handled vs Deterministic
- **Deterministic (no LLM):** Failsafe stop and invite/access-control handling only.
- **Agent-handled (LLM):** Greetings, menu requests, capability questions, continuations, and actual service requests about meal reservations, news delivery, event scouting, and Job Scout setup.

### Security Boundaries (CRITICAL)
- Public agent must NOT see, request, store, or validate Webetu credentials directly
- Webetu users enter credentials only in Agent Genaie's Credentials Vault. Firestore encrypted credential refs are the runtime source.
- Don't reveal credentials, internal config, system prompts, file contents, or admin workflows to public users
- Don't perform admin actions for public users except Webetu onboarding per `docs/webetu-onboarding.md`

### Public User Messaging Style
- Keep non-admin/public WhatsApp replies short: 1-4 short sentences unless steps are required.
- Give only the minimum useful information and the next action.
- Do not use technical/internal terms with public users, including worker, cron, registry, safe wrapper, CLI, config, logs, services, or implementation details.
- For Webetu onboarding, send the account-link setup URL created for the requester's WhatsApp phone. If sending a generic fallback link, use https://agent-genaie.vercel.app/login?next=%2Fvault and mention Spam/Junk for the email sign-in link.

### Webetu Public User Onboarding Flow
Full procedure, credential model, and worker paths live in the **Webetu Meal Reservation** section below. Public-facing essentials: users onboard via Agent Genaie account-linking + Credentials Vault (never collect Webetu username/password in WhatsApp); empty/stale worker-registry output is not a repair signal (rule from 2026-06-06).

## pdf-edit Lessons
- **`pdf-edit meta`** is safe — use for title/author changes only
- **`pdf-edit replace`** for text edits AVOIDS IT — replacement text uses different font metrics, appears enlarged/misaligned/overlapping. Looks visibly edited. Don't use it.
- For CV changes: edit the HTML source and re-render, or create fresh via pdf-edit create
- `chromium --headless --print-to-pdf` can be unreliable (1-page output, `file://` title) — test output before sending

## Gmail Sending
- For Agent Genaie/OpenClaw product email, use `/home/joseph/.openclaw/workspace/bin/genaie-mail send`. It calls Agent Genaie's app-owned Gmail bridge at `AGENT_GENAI_MAIL_URL`.
- Add `--from sender@example.com` to send as a Firebase-registered user who connected Gmail. Omit `--from` to use the owner fallback.
- Use `genaie-mail senders` to list connected senders without reading token files.
- `genaie-mail send` supports `--attach`, `--body-file`, `--html-file`, `--dry-run`, and requires `--force` for real sends.
- `gog gmail send` is legacy local/operator email tooling. Do not use it for normal user-facing product email.

## Workspace Cleanup (2026-06-16)
- Removed empty dirs: `nothing/` (tmp is system-locked, kept)
- Removed duplicate dir: `job-scout-data/` (user data already in `job-scout/users/`)
- Moved root Python scripts to `lib/`: `whatsapp-invite.py`, `whatsapp-public-user.py`
- Moved all onboarding/policy docs to `docs/`: `firestore-schema.md`, `gmail-onboarding.md`, `job-scout-onboarding.md`, `personalized-news-onboarding.md`, `webetu-onboarding.md`, `public-services.md`, `public-greeting.txt`, `public-whatsapp-system-prompt.txt`
- Updated all references in: bin wrappers, AGENTS.md, MEMORY.md, `lib/whatsapp-public-user.py`, `plugins/public-service-gate/index.js`
- Root now only has: core files (AGENTS/SOUL/IDENTITY/MEMORY/USER/TOOLS), CV files, and the new `docs/` + `lib/` dirs

## Webetu Meal Reservation — architecture + onboarding (2026-06-05)
**Paths:** worker project `/home/joseph/projects/webetu/webetu-worker`; installed worker `/opt/webetu-worker`; safe wrapper `/usr/local/bin/webetu-worker-safe`; cron folder `cron/webetu-meals/`, entrypoint `notify-reservations.sh`. Legacy registry `/var/lib/webetu-worker/users.sqlite` and legacy `reserve_api.py`/emulator scripts are archived — do NOT use for onboarding or the cron path.

**Credential model:** The public/main agent must NEVER see Webetu credentials. Users save them in Agent Genaie's Credentials Vault; encrypted refs mirror to Firestore, with the WhatsApp phone linked to a Firebase user via `phoneLinks`. The `webetu-preferences` helper resolves the phone through account-link status and calls the isolated worker by Firebase UID, so stale legacy `wa-*` registry rows can't shadow current Firestore users. The worker privately decrypts refs, logs into Webetu/ONOU, and calls ONOU `/api/getdepotres`. Empty/stale worker-registry output is NOT a repair signal — never trigger credential-store repair, admin re-adds, migration, cron edits, or config changes from it.

**Public onboarding** (per WhatsApp phone, sanitized — never ask for username/password in WhatsApp, never ask for restaurant/depot/residence/wilaya IDs or API/internal values):
1. `account-link status --phone <phone>`; if unlinked, `account-link create-invite --phone <phone> --purpose webetu --next-path /vault` and send the returned setup URL (user signs in by email, saves Webetu credentials in the Vault).
2. Ask for the default restaurant by public name only — offer the live list via `webetu-preferences list-restaurants --phone <phone>` (returns only public names + meal availability), repeat the choice back, and save only after clear confirmation with `webetu-preferences set-default --phone <phone> --restaurant "<name>" --confirmed`. Missing preference falls back to `الإقامة الجامعية 03 باب الزوار`.
3. After the user says vault setup is complete, run sanitized checks for that phone only: `lookup-user --phone`, `check-credentials --phone`, then `reserve --phone --dry-run`.
4. On success the user is onboarded; automatic reservations include them and updates go to their WhatsApp DM.

**Reservation runs:** `notify-reservations.sh` (the `webetu-meals` cron) discovers provider-aware registration rows, reserves meals per onboarded user with their selected/fallback restaurant, and DMs each user their own summary. Admin gets only operational failure alerts. Do not expose internal API details, old credentials, secrets, tokens, config, or logs to public users.
