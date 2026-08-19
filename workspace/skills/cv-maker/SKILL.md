---
name: cv-maker
description: Make a professional one-page CV for a public WhatsApp user, by revamping an uploaded CV or building one from a few questions, and send the finished PDF back in the chat.
---

# cv-maker

Use this skill for the standalone CV service: a public WhatsApp user just wants a
professional CV made, without enrolling in Job Scout. There is no account link, no
Gmail, no saved profile, and nothing is stored. You collect the CV content, run one
command, and it renders a clean one-page PDF and sends it straight back to the user.

This is the CV **document** service, separate from Job Scout (finding and applying to
jobs). If the user actually wants jobs found and applied to, use the job-scout skill
instead.

## Two paths

Ask the user which they want:

1. **Revamp an uploaded CV** — the user sends their current CV as a PDF. Reformats it
   into the professional template.
2. **Build from scratch** — the user answers a few short questions and you compose the
   content.

Ask upload-vs-questions first. Ask at most one or two questions per message. Only read
or extract from an uploaded CV after the user gives permission.

## Revamp path (uploaded CV)

When the user uploads their CV, run:

```
/home/joseph/.openclaw/workspace/bin/job-scout make-cv --phone "<whatsapp phone>" --cv "<MediaPath>"
```

`<MediaPath>` is the uploaded file's temporary path. The CV must be a PDF. The command
extracts the content, rebuilds it in the professional template, renders a validated
one-page PDF, and sends it to the user on WhatsApp. Do not stage or upload the CV
anywhere first — this service keeps nothing.

## Build-from-scratch path (a few questions)

Collect, over one or two short messages: full name; contact (phone, email, city/area);
an optional short professional headline/summary; key skills; work experience (role,
employer, dates, a few achievements each); education; and optionally languages.

Then write the details to a plain text file in the canonical CV shape and run:

```
/home/joseph/.openclaw/workspace/bin/job-scout make-cv --phone "<whatsapp phone>" --details-file "<path to details.txt>"
```

The details file format the command expects:

- Line 1 is the person's full name.
- An optional professional headline can follow on the next line.
- A blank line, then each section as an UPPERCASE heading on its own line followed by
  its content lines. `CONTACT`, `SKILLS`, and `LANGUAGES` render in the sidebar;
  `PROFILE`/`SUMMARY`, `EXPERIENCE`, `EDUCATION`, etc. render in the main column.
- Under `SKILLS`, put one skill per line. Under `EXPERIENCE`, start each achievement
  line with a dash so it renders as a bullet.

Example details file:

```
Joseph Munemo
Software Engineer

CONTACT
+263 77 000 0000
joseph@example.com
Bindura, Zimbabwe

SKILLS
Python
JavaScript
SQL

PROFILE
Backend engineer with four years building reliable web services.

EXPERIENCE
Software Engineer, Acme Ltd (2021 - present)
- Built and shipped the payments API used by 40k users
- Cut request latency by 45% with query and cache work

EDUCATION
BSc Computer Science, University of Zimbabwe (2020)

LANGUAGES
English - Fluent
Shona - Native
```

## Rules

- Never invent facts. Use only what the user uploaded or told you. If a section is
  missing, ask a short question or leave it out — do not fabricate employers, dates,
  or qualifications.
- The command owns delivery: it sends the finished PDF back to the user automatically.
  After it succeeds, confirm briefly (e.g. "Sent your CV — good luck!"). Do not describe
  files, paths, page counts, or internal steps.
- Keep replies short and WhatsApp-friendly. Never expose file paths, command output,
  or other internal detail.
- No account, sign-in, Gmail, or payment is required or requested for this service.
