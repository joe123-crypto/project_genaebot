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

Interview the user like a disciplined assistant: gather one field at a time, then write
everything to a plain text file in the canonical CV shape and render it. This produces a
FINAL PDF with no later tailoring, so you must collect the full picture: name, contact,
an optional headline/summary, skills, work experience with achievements, education, and
languages.

Rules:

- Be brief. No greetings, no filler, no praise, and never restate their answers.
- Ask for only ONE piece of information per turn. You may combine at most TWO closely
  related fields (for example a start and end date), but never three or more.
- Never ask for email, phone, and city in the same question. Ask for each in its own
  turn.
- Collect a work role across several short turns (title, then employer, then dates, then
  achievements), never as one big multi-part question.
- Never re-ask or re-confirm anything already collected; move straight to the next
  missing field.
- Accept "skip", "none", or "done" to move past optional sections or to stop adding more
  items.
- Skills, work experience, education, and languages are lists: keep asking "another?"
  until the user is done, then move on.
- Full name is required; other fields are optional and can be left out if the user has
  nothing to add.

Suggested wording:

- Opening: "Hi! I'll build your CV just by chatting, no long forms. First up: what's
  your full name?"
- Contact, each its own turn: "What's your phone number?" / "And your email address?" /
  "Which city or area are you in?"
- Headline: "Want a short professional headline or summary? Type a sentence, or 'skip'."
- Skills, one per turn: "What's a key skill of yours?" → "Another skill, or type
  'done'?"
- Experience, per role across turns: "What was your most recent job title?" → "Which
  employer?" → "What dates? For example 'Jan 2020 to present'." → "Give an achievement or
  two from that role (start each with a dash), or 'skip'." → "Any other roles? Give the
  job title, or type 'done'."
- Education: "What's your most recent qualification?" → "Where from?" → "What year, or
  dates?" → "Any others, or type 'done'?"
- Languages (optional): "Any languages to list? For example 'English - Fluent', or
  'skip'." → "Another, or 'done'?"
- Close: confirm you'll now generate their CV.

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
