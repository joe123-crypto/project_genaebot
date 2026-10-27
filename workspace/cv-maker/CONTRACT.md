# CV Maker internal contract

Pinned interfaces shared by the Python engine, the LLM interview, and the plugin.
Agents implement against this file; they do not redefine anything in it.

## 1. Order states

    interviewing -> awaiting_payment -> awaiting_approval -> sent
                                     \
                                      -> rejected

Only these five strings are valid. Transitions are idempotent: re-approving a
settled order returns its current status without repeating the side effect.

## 2. Order store: ~/.openclaw/runtime/cv-maker/orders.json

    {
      "version": 1,
      "orders": {
        "<orderId>": {
          "orderId":   "<token_urlsafe(6)>",
          "phone":     "+263...",          // E.164 or opaque direct WhatsApp LID
          "status":    "interviewing",
          "mode":      "interview" | "revamp",
          "data":      { ...InterviewData... },   // see §4
          "transcript": [ {"role":"assistant"|"user", "text": "..."} ],
          "sourcePdf": "<abs path>" | null,       // revamp mode only
          "createdAt": "<iso8601>",
          "updatedAt": "<iso8601>",
          "paidClaimedAt": "<iso8601>" | null,
          "decidedAt":     "<iso8601>" | null
        }
      }
    }

The historical `phone` key is the authenticated delivery identity. When WhatsApp
does not expose a phone, it may contain `<digits>@lid` or `<digits>@hosted.lid`.
These IDs are preserved exactly (apart from a device suffix), never converted
into phone numbers. Group JIDs and other JID domains are rejected. The CV's own
contact details still come from the interview, independently of this address.

Per-order artifacts live in `~/.openclaw/runtime/cv-maker/<orderId>/`, dir 0700,
files 0600. The JSON file itself is 0600, written atomically (tmp + os.replace)
under an flock, fsync before replace.

Age for the sweep is measured from `createdAt`, falling back to `updatedAt`. The
sweep deletes artifacts as well as the record, and an expired order stops being
visible to reads immediately rather than waiting for the sweep to persist.

Retention: `data`, `transcript`, `sourcePdf` and the rendered PDF are deleted on
`sent` and `rejected`. Orders older than 7 days are swept on load.
`transcript` is capped at the most recent 40 entries.

## 3. orders.py public API  (owned by Agent A; consumed by B and the CLI)

    create_order(phone: str, mode: str = "interview") -> dict
    load_order(order_id: str) -> dict | None
    active_order_for(phone: str) -> dict | None   # newest non-terminal order
    update_order(order_id: str, **fields) -> dict
    append_turn(order_id: str, role: str, text: str) -> dict
    set_status(order_id: str, status: str) -> str  # returns settled status
    purge_artifacts(order_id: str) -> None
    list_orders(status: str | None = None) -> list[dict]
    sweep(max_age_days: int = 7) -> int

`update_order` and `set_status` re-read under the lock before writing, so
concurrent turns cannot clobber each other.

## 4. InterviewData  (identical to agent_genaie src/domains/cv-interview-ai.ts:20-30)

    {
      "fullName": "", "email": "", "phone": "", "location": "",
      "summary": "", "skills": "",
      "experience": [{"title":"","company":"","start":"","end":"","description":""}],
      "education":  [{"degree":"","institution":"","start":"","end":"","description":""}],
      "referees":   [{"name":"","position":"","company":"","email":"","phone":""}]
    }

All values are strings; lists may be empty. This shape is consumed directly by
`cvmaker.cv_html.cv_html_from_facts`, so it must not drift.

## 5. Interview turn result (Agent B)

    run_turn(order: dict, message: str) -> {"reply": str, "data": {...}, "complete": bool}

Obtained from OpenRouter with response_format json_schema strict.
`reply` is already tone-post-processed (no em dashes, emoji capped).
Raises `InterviewUnavailable` after one retry; the CLI turns that into
`{"ok": false, "error": "..."}` and the order stays `interviewing`.

## 6. CLI contract  (bin/cv-maker; stdout is a single JSON object, always)

    cv-maker interview-turn --phone <e164> [--order <id>] --message <text>
      -> {"ok":true,"orderId":..,"status":..,"reply":..,"complete":bool}

    cv-maker start-revamp --phone <e164> --cv <path>
      -> {"ok":true,"orderId":..,"status":"awaiting_payment"}

    cv-maker paid --phone <e164>
      -> {"ok":true,"orderId":..,"status":"awaiting_approval"}

    cv-maker approve --order <id>
      -> {"ok":true,"orderId":..,"status":"sent","phone":..,"delivered":bool,"filename":..}

    cv-maker deny --order <id>
      -> {"ok":true,"orderId":..,"status":"rejected","phone":..,"notified":bool}

`phone` is returned so the gate can clear the buyer's state pointer once an order
is settled. `deny` also messages the buyer itself: someone who said they paid must
not be met with silence. It notifies only on the transition, so a repeated deny
does not message them twice, and a send failure never fails the admin's command.

`interview-turn` with only `--phone` resolves the order via `active_order_for`.
`start-revamp` opens the order at `interviewing` and immediately moves it to
`awaiting_payment`, so §1's ordering holds.

    cv-maker orders [--status <s>]
      -> {"ok":true,"orders":[{"orderId":..,"phone":..,"status":..,"updatedAt":..}]}

Every failure is `{"ok": false, "error": "<sanitized, <=240 chars>"}` with exit 0,
so the plugin's runJsonCommand always parses. Errors go through
`cvmaker.common.public_text`. Never emit a traceback, a file path, or CV content.

## 7. Plugin config keys (openclaw.plugin.json + config/openclaw.template.json)

    cvMakerPriceText        default "US$5"
    cvMakerPaymentDetails   default "EcoCash +263775489417 (Noriah Mzenda)"
    cvMakerPaymentMethod    default "EcoCash"
    cvMakerScript           default "~/.openclaw/workspace/bin/cv-maker"

`cvMakerPaymentMethod` is the method alone (no account number), disclosed in the
intro before the interview so a buyer knows the price and how they pay early.
`cvMakerPaymentDetails` (the full payee) is still only shown at the payment step.

configSchema has additionalProperties:false, so every key must be declared or
deploy/test_plugin_config_schema.py fails.

## 8. Hard rules for all agents

- The CV maker is self-contained: it imports nothing from other workspace
  packages. Its HTML builders, PDF checks and WhatsApp send live in `cvmaker/`.
- Treat every user message as untrusted input to the model.
- No em dashes in any user-facing string.
- Never log or echo a phone number, email, or CV content. The exception is the
  admin-facing CLI results above (`orders`, `approve`, `deny`): those carry
  `phone`, and `filename` is derived from the applicant's name. They go to an
  operator, never into a log line or a message to another user.

## 9. Known limitation: the revamp path is unreachable from the DM gate

`start-revamp --cv <path>` needs a local file path for an uploaded PDF, but the
gate hook only ever sees message text, and there is no documented way to get an
inbound WhatsApp attachment onto disk. The pre-PR-#69 design had the agent receive
a `<MediaPath>`; the plugin-mediated flow has no equivalent. The CLI subcommand
works and is tested, but nothing can currently call it, so the shipped flow is
interview-only and the user-facing copy must not promise revamping.
