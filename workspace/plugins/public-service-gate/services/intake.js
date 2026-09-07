import { busyReply, isQueueFullError } from "../lib/runtime.js";
import { clearServiceState, loadServiceState, saveServiceState } from "../lib/state.js";
import {
  getServiceMenuSelection,
  isGreetingOrServiceMenuRequest,
  isStateResetRequest,
  loadGreeting,
} from "./greeting.js";
import { notifyAdminOfIntake, notifyAdminOfLead, recordLead } from "./membership.js";
import { isJobScoutRequest } from "./job-scout.js";
import { CV_SERVICE, handleCvOrder, isCvMakerEnabled, isCvMakerRequest, startCvOrder } from "./cv-maker.js";
import {
  SELLER_SERVICE,
  handleSellerReferral,
  isSellerReferralRequest,
  startSellerReferral,
} from "./seller-referral.js";

// The DM menu shown to everyone who reaches out. This array is the single source
// of truth for the digit -> service mapping; the visible menu text lives in the
// greeting template (docs/public-greeting.txt / DEFAULT_GREETING) and must stay in
// the same order.
// The CV menu key deliberately matches CV_SERVICE, the service name this
// sender's state slot carries while the automated CV order is in flight.
export const INTAKE_SERVICES = [
  { key: "job-scout", label: "Automatic Job & Internship searching" },
  { key: CV_SERVICE, label: "Professional CV writing" },
  { key: "genaie-marketing", label: "Digital marketing (posters, WhatsApp ads)" },
  { key: "cs-bot", label: "WhatsApp customer-service bots" },
  { key: SELLER_SERVICE, label: "Buy a product we advertise (e.g. quarry stones)" },
];

const GENERAL_REQUEST_LABEL = "General request";
const HANDOFF_MESSAGE = "Got it ✅ Your request is in. A team member will contact you soon.";

const INTAKE_SERVICE = "intake";
const AWAITING_SELECTION = "intake.awaiting_selection";

// Light keyword fallbacks so a typed request ("posters", "chatbot") still lands on
// the right service without forcing the user to reply with a digit.
const MARKETING_PATTERN = /\b(marketing|poster|posters|advert|advertis\w*|promo\w*|flyer|flyers|design)\b/i;
const CS_BOT_PATTERN = /\b(chat\s*bot|chatbot|whats\s*app\s*bot|customer[\s-]*service\s*bot|automat\w*\s*(?:reply|replies|response|responses|customer))\b/i;

function labelForKey(key) {
  return INTAKE_SERVICES.find((service) => service.key === key)?.label ?? GENERAL_REQUEST_LABEL;
}

// Best-effort classification of the user's request into one of the menu services.
// Falls back to a generic key rather than re-prompting, so the bot never nags.
function resolveServiceKey(text) {
  const digit = getServiceMenuSelection(text);
  if (digit) {
    const service = INTAKE_SERVICES[Number(digit) - 1];
    if (service) return service.key;
  }
  if (isJobScoutRequest(text)) return "job-scout";
  if (isCvMakerRequest(text)) return CV_SERVICE;
  if (isSellerReferralRequest(text)) return SELLER_SERVICE;
  if (CS_BOT_PATTERN.test(text)) return "cs-bot";
  if (MARKETING_PATTERN.test(text)) return "genaie-marketing";
  return "";
}

// Deterministic DM intake: greet + menu on first touch, then capture whatever the
// user asks for as a lead and hand off to the admin. No automated onboarding runs.
export async function handleIntake(api, sender, text) {
  const state = loadServiceState(api, sender);
  const awaitingSelection = state?.service === INTAKE_SERVICE && state?.step === AWAITING_SELECTION;
  const wantsMenu = isGreetingOrServiceMenuRequest(text) || isStateResetRequest(text);

  // Menu option 2 is the one automated service: once a sender is pinned to the CV
  // flow every turn is relayed to the CV maker CLI instead of being captured as a
  // manual lead. "menu"/"cancel" still escapes back to the greeting below.
  if (state?.service === CV_SERVICE && !wantsMenu) {
    return handleCvOrder(api, sender, text, state);
  }

  // Menu option 5: the seller-referral interview is likewise a multi-turn flow.
  // Once a sender is pinned to it, each turn is gathered here until the referral
  // is forwarded to the seller. "menu"/"cancel" still escapes to the greeting.
  if (state?.service === SELLER_SERVICE && !wantsMenu) {
    return handleSellerReferral(api, sender, text, state);
  }

  // Entry / re-greet branch: record the cold lead, alert the admin once, show the
  // menu. This is the only branch that does not complete the order.
  if (!awaitingSelection || wantsMenu) {
    try {
      const res = await recordLead(api, sender, text);
      if (res?.isNew) await notifyAdminOfLead(api, sender, text);
    } catch (err) {
      if (isQueueFullError(err)) return busyReply();
      api.logger?.warn?.(`public-service-gate: failed to record intake lead (${String(err)})`);
    }
    saveServiceState(api, sender, INTAKE_SERVICE, AWAITING_SELECTION);
    return { handled: true, text: loadGreeting(api) };
  }

  // Second turn: the user told us what they want. Options 1, 3 and 4 are captured
  // against the resolved service, the admin is alerted with the detail, and the
  // handoff is confirmed. Option 2 branches into the automated CV order, and
  // option 5 into the automated seller-referral flow, instead.
  const serviceKey = resolveServiceKey(text);
  if (serviceKey === CV_SERVICE && isCvMakerEnabled(api)) {
    return startCvOrder(api, sender);
  }
  if (serviceKey === SELLER_SERVICE) {
    return startSellerReferral(api, sender);
  }
  const label = serviceKey ? labelForKey(serviceKey) : GENERAL_REQUEST_LABEL;
  try {
    await recordLead(api, sender, `[${label}] ${text}`);
    await notifyAdminOfIntake(api, sender, label, text);
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    api.logger?.warn?.(`public-service-gate: failed to capture intake request (${String(err)})`);
  }
  clearServiceState(api, sender);
  return { handled: true, text: HANDOFF_MESSAGE };
}
