import { expandHome, getPluginConfig } from "../lib/config.js";
import {
  HELPER_TIMEOUT_MS,
  busyReply,
  helperLimiter,
  isQueueFullError,
  runJsonCommand,
  sanitizeLogText,
} from "../lib/runtime.js";
import { clearServiceState, saveServiceState } from "../lib/state.js";
import { notifyAdminOfIntake } from "./membership.js";

const CV_MAKER_UNAVAILABLE = "The CV maker isn't available here right now. Try again later.";

// Defaults mirror openclaw.plugin.json / config/openclaw.template.json. The
// payment destination is configuration, so it is never written anywhere else.
const DEFAULT_CV_PRICE_TEXT = "US$5";
const DEFAULT_CV_PAYMENT_DETAILS = "EcoCash +263783496308 (Charlene Anika)";

// Intent to have a CV/résumé created or revamped. This is about the CV DOCUMENT
// itself, distinct from Job Scout (which is about finding and applying to jobs),
// so these patterns intentionally key on cv/résumé/resume plus a make/revamp verb
// and never on "jobs"/"apply".
// The verb stems carry a trailing \w* so inflected forms match too (revamp ->
// revamped/revamping, write -> writing, and the writ/wrote stems cover written).
const CV_MAKER_REQUEST_PATTERNS = [
  /\b(?:make|create|build|writ|wrote|design|revamp|redo|redesign|update|fix|improve|format|polish|prepare)\w*\b[^.!?]*\b(?:cv|résumé|resume|curriculum\s+vitae)\b/i,
  /\b(?:cv|résumé|resume|curriculum\s+vitae)\b[^.!?]*\b(?:make|create|build|writ|wrote|design|revamp|redo|redesign|update|fix|improve|format|polish|prepare|help|need|want|service|maker|builder)\w*\b/i,
  /\b(?:cv|résumé|resume)\s+(?:maker|builder|writer|writing|service|help)\b/i,
];

export function isCvMakerRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 420) return false;
  return CV_MAKER_REQUEST_PATTERNS.some((pattern) => pattern.test(compact));
}

function cvMakerIntro() {
  return `I can make you a clean, professional one-page CV right here. Two ways:

1. Send me your current CV as a PDF and I'll revamp it into a professional format.
2. Answer a few quick questions and I'll build one from scratch.

Which would you like?`;
}

// Classic allowlist mode only: the `cv_maker_start` agent tool. The open-DM gate
// never reaches this, it runs the automated order flow below instead.
export async function handleCvMakerRequest(api, sender, text = "") {
  if (getPluginConfig(api).cvMakerEnabled !== true) {
    return { handled: true, text: CV_MAKER_UNAVAILABLE };
  }
  // The deterministic entry point just offers the two paths. The agent, guided by
  // the cv-maker skill/doc, collects the CV (an uploaded PDF or a short question
  // set), runs `job-scout make-cv`, and that command renders and sends the
  // finished PDF back on WhatsApp. No account link, Gmail, or stored profile.
  //
  // Deliberately no saveServiceState here: lib/state.js keeps a single slot per
  // phone, so writing one would evict a Job Scout setup already in flight for
  // this sender. The agent continues this flow from conversation context, and
  // nothing reads a "cv-maker" step, so there is no state worth that cost.
  return { handled: true, text: cvMakerIntro() };
}

// ---------------------------------------------------------------------------
// Paid CV order flow (open-DM gate mode)
//
// Menu option 2 branches out of the manual handoff and into an automated order
// driven by the Python CLI (see workspace/cv-maker/CONTRACT.md §6). The durable
// interview record, the transcript and the payment state all live in the Python
// order store; lib/state.js keeps ONE slot per phone with a 30 minute TTL, so it
// is used here only as a lightweight "this sender is in the CV flow" pointer.
// ---------------------------------------------------------------------------

export const CV_SERVICE = "cv";
export const CV_STEP_INTERVIEW = "cv.interviewing";
export const CV_STEP_PAYMENT = "cv.awaiting_payment";
export const CV_STEP_APPROVAL = "cv.awaiting_approval";

const DEFAULT_CV_MAKER_SCRIPT = "~/.openclaw/workspace/bin/cv-maker";

// An interview turn is an LLM round trip and an approval renders a PDF, so both
// need far more headroom than the 15s HELPER_TIMEOUT_MS the other helpers use.
export const CV_TURN_TIMEOUT_MS = 60000;
export const CV_APPROVE_TIMEOUT_MS = 120000;

const CV_MAKER_FAILED = "I couldn't reach the CV maker. Try again later.";
const CV_DECISION_FAILED = "That CV order couldn't be updated. Try again later.";
const CV_REVIEW_PENDING = "Your payment is with our team. You'll get your CV here once it's approved.";
const CV_PAID_ACK = "Thanks 🙏 We're confirming your payment. Your CV will arrive here once it's approved.";

const CV_INTRO = `Great, let's build your CV 📄

I'll ask you a few quick questions, then send you the price and payment details.

Reply with your full name to begin.`;

// Deliberately narrow: only a short, unambiguous "I have paid" claim moves the
// order on. Anything else in the payment step gets the details re-sent.
const PAID_CLAIM_PATTERN =
  /^(?:paid|i\s*(?:have|'ve)?\s*paid|payment\s+(?:sent|made|done)|i\s+sent\s+(?:it|the\s+(?:money|payment))|money\s+sent)[\s!.]*$/i;

// Admin decision command, mirroring parseAllowCommand's strict anchored shape.
// Order ids are token_urlsafe(6), so URL-safe base64 characters only.
// The id must start alphanumeric, matching orders.ORDER_ID_RE. Without that
// anchor "cv approve --anything" would satisfy the character class and inject
// an argparse flag into the CLI call below.
const CV_DECISION_PATTERN = /^cv\s+(approve|deny)\s+([A-Za-z0-9][A-Za-z0-9_-]{5,11})$/i;

export function isCvMakerEnabled(api) {
  return getPluginConfig(api).cvMakerEnabled === true;
}

export function getCvMakerScript(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.cvMakerScript === "string" && pluginCfg.cvMakerScript.trim()
      ? pluginCfg.cvMakerScript.trim()
      : DEFAULT_CV_MAKER_SCRIPT;
  return expandHome(configuredPath);
}

function configText(api, key, fallback) {
  const value = getPluginConfig(api)[key];
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

// Price and payment destination are configuration, never literals in the code.
export function cvPaymentText(api) {
  return `Price: ${configText(api, "cvMakerPriceText", DEFAULT_CV_PRICE_TEXT)}
Pay to: ${configText(api, "cvMakerPaymentDetails", DEFAULT_CV_PAYMENT_DETAILS)}

Reply "paid" once you've sent it.`;
}

export function isCvPaidClaim(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 40) return false;
  return PAID_CLAIM_PATTERN.test(compact);
}

export function parseCvDecisionCommand(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 40) return null;
  const match = CV_DECISION_PATTERN.exec(compact);
  if (!match) return null;
  return { action: match[1].toLowerCase(), orderId: match[2] };
}

function logCvFailure(api, phase, err) {
  const status = err?.status ?? err?.code;
  const statusPart = status === undefined ? "" : ` status=${status}`;
  const message = sanitizeLogText(err?.message ?? String(err));
  const stderr = sanitizeLogText(err?.stderr);
  const stderrPart = stderr ? ` stderr="${stderr}"` : "";
  api.logger?.warn?.(
    `public-service-gate: cv maker failure phase="${phase}" script=${getCvMakerScript(api)}${statusPart} error="${message}"${stderrPart}`,
  );
}

function runCvMaker(api, args, timeoutMs) {
  return helperLimiter(() => runJsonCommand(getCvMakerScript(api), args, { timeoutMs }));
}

// The CLI's own `error` string is the only failure text we ever show: the
// contract guarantees it is already run through public_text and carries no
// traceback, path or CV content. Anything else gets the generic message.
function cliErrorText(result, fallback = CV_MAKER_FAILED) {
  const error = typeof result?.error === "string" ? result.error.trim() : "";
  if (!error) return fallback;
  return error.slice(0, 240);
}

// Entry point for menu option 2 / a CV request. Only pins the sender to the CV
// flow; the order itself is created by the first relayed interview turn.
// Which step a status belongs to. The order store is the source of truth; the
// state slot is only a hint, and it expires in 30 minutes while a payment takes
// hours or days.
const STEP_FOR_STATUS = {
  awaiting_payment: CV_STEP_PAYMENT,
  awaiting_approval: CV_STEP_APPROVAL,
};

// Read the sender's live order so a returning buyer resumes where they actually
// are. Without this a buyer whose pointer lapsed while paying is put back into
// an interview, and the CLI then refuses every message because their order has
// moved past that stage: they have paid and can never say so.
async function resolveCvStep(api, sender) {
  try {
    const result = await runCvMaker(api, ["status", `--phone=${sender}`], HELPER_TIMEOUT_MS);
    if (result?.ok !== true) return CV_STEP_INTERVIEW;
    return STEP_FOR_STATUS[String(result.status ?? "")] ?? CV_STEP_INTERVIEW;
  } catch (err) {
    if (isQueueFullError(err)) throw err;
    logCvFailure(api, "status lookup", err);
    return CV_STEP_INTERVIEW;
  }
}

export async function startCvOrder(api, sender) {
  let step = CV_STEP_INTERVIEW;
  try {
    step = await resolveCvStep(api, sender);
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    throw err;
  }
  saveServiceState(api, sender, CV_SERVICE, step);
  if (step === CV_STEP_PAYMENT) return { handled: true, text: cvPaymentText(api) };
  if (step === CV_STEP_APPROVAL) return { handled: true, text: CV_REVIEW_PENDING };
  return { handled: true, text: CV_INTRO };
}

async function relayInterviewTurn(api, sender, text) {
  let result;
  try {
    result = await runCvMaker(
      api,
      // Single-argv "--opt=value" form: the message is whatever the user typed,
      // and argparse reads an option-lookalike token ("--skip", "-n/a") as a flag
      // when it arrives as its own argv entry, breaking the turn.
      ["interview-turn", `--phone=${sender}`, `--message=${String(text ?? "")}`],
      CV_TURN_TIMEOUT_MS,
    );
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    logCvFailure(api, "interview turn", err);
    return { handled: true, text: CV_MAKER_FAILED };
  }
  if (result?.ok !== true) return { handled: true, text: cliErrorText(result) };

  const reply = typeof result.reply === "string" && result.reply.trim() ? result.reply.trim() : "";
  if (result.status === "awaiting_payment") {
    saveServiceState(api, sender, CV_SERVICE, CV_STEP_PAYMENT);
    const payment = cvPaymentText(api);
    return { handled: true, text: reply ? `${reply}\n\n${payment}` : payment };
  }
  // Refresh the pointer so a long interview does not age out of the 30 min TTL.
  saveServiceState(api, sender, CV_SERVICE, CV_STEP_INTERVIEW);
  return { handled: true, text: reply || CV_MAKER_FAILED };
}

async function claimCvPayment(api, sender) {
  let result;
  try {
    result = await runCvMaker(api, ["paid", `--phone=${sender}`], HELPER_TIMEOUT_MS);
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    logCvFailure(api, "payment claim", err);
    return { handled: true, text: CV_MAKER_FAILED };
  }
  if (result?.ok !== true) return { handled: true, text: cliErrorText(result) };

  saveServiceState(api, sender, CV_SERVICE, CV_STEP_APPROVAL);
  const orderId = String(result.orderId ?? "").trim();
  // notifyAdminOfIntake already loops every configured admin and is best effort,
  // so a messaging outage never blocks the user's acknowledgement.
  await notifyAdminOfIntake(
    api,
    sender,
    `CV order ${orderId} payment claimed`,
    `Reply: cv approve ${orderId} or cv deny ${orderId}`,
  );
  return { handled: true, text: CV_PAID_ACK };
}

// One user turn inside the CV flow. `state` is the sender's service-state slot,
// already known to belong to this flow.
export async function handleCvOrder(api, sender, text = "", state = null) {
  if (!isCvMakerEnabled(api)) {
    clearServiceState(api, sender);
    return { handled: true, text: CV_MAKER_UNAVAILABLE };
  }
  let step = state?.step ?? CV_STEP_INTERVIEW;

  // A claim of payment is judged against the order, never against the pointer.
  // The pointer expires in 30 minutes and paying takes longer, so a buyer who
  // comes back is otherwise put into a fresh interview and can never tell us
  // they paid. Resolving here costs one lookup and only on a paid claim.
  if (step === CV_STEP_INTERVIEW && isCvPaidClaim(text)) {
    try {
      step = await resolveCvStep(api, sender);
    } catch (err) {
      if (isQueueFullError(err)) return busyReply();
      throw err;
    }
    saveServiceState(api, sender, CV_SERVICE, step);
  }

  if (step === CV_STEP_APPROVAL) return { handled: true, text: CV_REVIEW_PENDING };
  if (step === CV_STEP_PAYMENT) {
    if (!isCvPaidClaim(text)) {
      return { handled: true, text: cvPaymentText(api) };
    }
    return claimCvPayment(api, sender);
  }
  return relayInterviewTurn(api, sender, text);
}

// Admin-only settlement. The caller is responsible for the isWhatsAppAdmin
// check: a non-admin "cv approve <id>" must never reach this function.
export async function handleCvDecision(api, decision) {
  const action = decision?.action === "deny" ? "deny" : "approve";
  const orderId = String(decision?.orderId ?? "");
  const timeoutMs = action === "approve" ? CV_APPROVE_TIMEOUT_MS : HELPER_TIMEOUT_MS;

  let result;
  try {
    // Single-argv "--order=<id>" form on purpose: passing the id as its own
    // argv entry lets a value beginning with "-" be parsed as a flag.
    result = await runCvMaker(api, [action, `--order=${orderId}`], timeoutMs);
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    logCvFailure(api, `order ${action}`, err);
    return { handled: true, text: CV_DECISION_FAILED };
  }
  if (result?.ok !== true) return { handled: true, text: cliErrorText(result, CV_DECISION_FAILED) };

  if (action === "deny") return { handled: true, text: `Order ${orderId}: denied.` };
  const delivered = result.delivered !== false;
  return {
    handled: true,
    text: delivered ? `Order ${orderId}: approved and sent.` : `Order ${orderId}: approved, but the CV could not be sent.`,
  };
}
