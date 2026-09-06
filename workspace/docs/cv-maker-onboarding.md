# CV Maker

Purpose: make a professional CV for a public WhatsApp user, for US$5, without enrolling them in
Job Scout. There is no account link, no Gmail, and no saved profile.

**This service runs deterministically inside the public-service-gate plugin. The agent does not
drive it and cannot run any part of it.** Menu option 2 hands the sender to the gate, which
interviews them, asks for payment, and delivers the CV once an admin approves. This document
describes the flow so an operator can reason about it; it is not a script for the agent to follow.

## Flow

1. The user picks option 2 from the DM menu, or asks for a CV in their own words.
2. The gate resolves whether they already have an order in flight and resumes them at the right
   point. A returning user is never restarted from scratch.
3. The intro message states the price and the payment method (for example "US$5, paid by EcoCash")
   before any questions are asked, so a user who does not want to pay, or cannot use the method,
   bounces up front rather than after answering the whole interview. The full payee (account number
   and name) is not shown yet.
4. It interviews them one field at a time, collecting name, contact, skills, work experience,
   education and referees. It does not ask for a professional summary or job descriptions: the model
   writes those itself from the facts given (with a confident, embellished framing), then shows them
   to the user to approve or edit. The interview is model-driven, so it copes with answers given out
   of order or several at once.
6. When the interview is complete the order moves to awaiting payment and the user is given the
   full payment details.
7. The user pays out of band and replies "paid". The order moves to awaiting approval and the
   configured admins are notified with the order id and the exact command to reply with.
8. An admin replies `cv approve <id>` or `cv deny <id>` in their own chat. Approval builds the CV,
   renders it to PDF and sends it to the user. A denial tells the user their payment could not be
   confirmed and leaves the conversation open.

## Payment

- Price and payee are configuration, not code: `cvMakerPriceText`, `cvMakerPaymentMethod` (the method
  alone, shown up front) and `cvMakerPaymentDetails` (the full payee, shown at the payment step) on
  the public-service-gate plugin. Change them there, never in a message. Keep `cvMakerPaymentMethod`
  consistent with the method named in `cvMakerPaymentDetails`.
- Payment is recorded by an admin. There is no gateway and no automatic verification, so approving
  an order is the only thing that releases a CV. Approve only after checking the money arrived.
- Nothing is built or rendered before approval, so an unpaid order costs nothing.

## Retention

The user's answers and the transcript are held while their order is open, and are deleted along with
the rendered PDF once the order is sent or rejected. Orders that were abandoned before payment are
swept after seven days. An order that is awaiting payment or awaiting approval is never swept: it
represents something owed to someone who may already have paid.

## Boundaries

- Never quote a different price, promise a CV before an admin has approved it, or say the service is
  free.
- Never invent an employer, date or qualification. If a detail is missing it is left out.
- Never expose helper output, file paths, logs, order internals, or another user's data.
- Only the configured WhatsApp admins can approve or deny. A non-admin sending an approval command
  is ignored entirely.

## Not currently supported: revamping an uploaded CV

The engine can rebuild a CV from an uploaded PDF, and that path is implemented and tested
(`cv-maker start-revamp --cv <path>`). It is unreachable from WhatsApp: the gate hook only ever sees
message text, and there is no supported way to get an inbound attachment onto disk. The menu
therefore offers "Professional CV writing" and does not mention revamping. If a media-download seam
is added later, re-enable it by wiring `start-revamp` to it and restoring the wording.
