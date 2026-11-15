# CV Maker

New WhatsApp CV orders are paused. The public-service-gate plugin handles
interviews, payment, and approval for orders already saved in the CV order store.

## Flow

1. The gate checks for an existing order on the receiving WhatsApp line. If a
   chat has lost its saved step, mentioning the CV order or replying "paid" can
   find the order again on that line.
2. An interview continues one field at a time, collecting name, contact, skills, work experience,
   education and referees. It does not ask for a professional summary or job descriptions: the model
   writes those itself from the facts given (with a confident, embellished framing), then shows them
   to the user to approve or edit. The interview is model-driven, so it copes with answers given out
   of order or several at once.
3. When the interview is complete the order moves to awaiting payment and the user is given the
   full payment details.
4. The user pays out of band and replies "paid". The order moves to awaiting approval and the
   configured admins are notified with the order id and the exact command to reply with.
5. An admin replies `cv approve <id>` or `cv deny <id>` in their own chat. Approval builds the CV,
   renders it to PDF and sends it to the user from the line where the order started. A denial
   uses that same line to tell the user their payment could not be confirmed and leaves the
   conversation open.

## Payment

- The payment step reads the configured price and payee from `cvMakerPriceText` and `cvMakerPaymentDetails` on
  the public-service-gate plugin.
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
- Continue serving customers whose phone number is not exposed by WhatsApp. Their chat identity
  stays attached to the order; digits from a chat identifier are never a substitute phone number.
- Only the configured WhatsApp admins can approve or deny. A non-admin sending an approval command
  is ignored entirely.

## Not currently supported: revamping an uploaded CV

The engine can rebuild a CV from an uploaded PDF, and that path is implemented and tested
(`cv-maker start-revamp --cv <path>`). It is unreachable from WhatsApp: the gate hook only ever sees
message text. The `start-revamp` CLI remains available to operators.
