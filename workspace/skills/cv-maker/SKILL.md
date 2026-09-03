---
name: cv-maker
description: Explains that the paid WhatsApp CV service runs inside the public-service-gate plugin and is not driven by the agent. Read this if you are about to try to make a CV for a public WhatsApp user.
---

# cv-maker

**There is nothing for you to do here.** The CV service runs deterministically inside the
public-service-gate plugin, end to end. If a public WhatsApp user wants a CV, the gate has already
handled it before you would ever see the message.

This skill exists only to stop the agent trying to run a flow that is no longer its to run.

## What the gate does

Menu option 2 pins the sender to the CV flow. The gate then interviews them, asks for US$5, notifies
an admin when they say they have paid, and sends the finished PDF once that admin approves. The
order and every answer live in the gate's own store.

## What you must not do

- Do not conduct the interview or ask a user for CV details yourself.
- Do not run any `cv-maker` or `job-scout` command to make a CV. The old `make-cv` command described
  by earlier versions of this skill no longer exists, and the public agent cannot execute commands
  in any case.
- Do not quote a price, promise a CV, or say when one will arrive. Only an admin approval releases a
  CV, and only the gate messages the user about it.
- Do not tell a user the service is free.

## Where the real documentation lives

`/home/joseph/.openclaw/workspace/docs/cv-maker-onboarding.md` describes the flow, the payment and
approval model, and the retention rules for operators.
