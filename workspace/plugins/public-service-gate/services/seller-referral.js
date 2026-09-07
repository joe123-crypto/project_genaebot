import { normalizePhone } from "../lib/events.js";
import { HELPER_TIMEOUT_MS, helperLimiter, runTextCommand } from "../lib/runtime.js";
import { clearServiceState, saveServiceState } from "../lib/state.js";

// ---------------------------------------------------------------------------
// Seller referral (menu option 5)
//
// Genae advertises other people's products. When a customer replies about an
// advert, this deterministic flow collects what they want and where they saw
// it, tells them the seller will be in touch, and forwards those details to the
// seller so they can contact the customer directly.
//
// The flow runs entirely in the plugin gate, never the LLM. That is deliberate:
// the bot only ever relays what the customer typed plus the fixed product name
// below, so it can never quote or invent a price and never invent any product
// detail. Do not route this through the agent.
//
// Product and seller are hardcoded for now (a single advertised product, one
// seller). If this grows to several products or sellers, lift these into config
// or a small registry keyed by product.
// ---------------------------------------------------------------------------

export const PRODUCT_NAME = "Quarry stones";

// The seller is reachable on two lines. The referral is delivered over WhatsApp,
// so it must target the WhatsApp-capable number. The other line takes phone
// calls only (and doubles as the operator/bot number), so a WhatsApp send there
// would never reach the seller.
export const SELLER_WHATSAPP = "+263775489417";
export const SELLER_PHONE_ONLY = "+263785632943";

export const SELLER_SERVICE = "seller-referral";
export const SELLER_STEP_DETAILS = "seller.awaiting_details";
export const SELLER_STEP_SOURCE = "seller.awaiting_source";

// Keep the forwarded snippets short and single-line, mirroring membership.js.
const SNIPPET_MAX = 280;
function snippet(text) {
  return String(text ?? "").replace(/\s+/g, " ").trim().slice(0, SNIPPET_MAX);
}

// Light keyword fallback so a typed request ("I saw your advert for quarry
// stones") lands on the referral flow without forcing the user to reply with a
// digit.
const SELLER_REFERRAL_PATTERN =
  /\b(quarry|stones?|advert|advertis\w*|saw\s+(?:your|the)\s+ad|the\s+seller)\b/i;

export function isSellerReferralRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 360) return false;
  return SELLER_REFERRAL_PATTERN.test(compact);
}

function introText() {
  return `Great — that's our ${PRODUCT_NAME.toLowerCase()} advert 🪨

What exactly are you looking for? (e.g. quantity, delivery location)`;
}

const SOURCE_QUESTION = "And where did you see the advert?";
const HANDOFF_MESSAGE = `Got it ✅ The seller will contact you soon about the ${PRODUCT_NAME.toLowerCase()}.`;

// Best-effort WhatsApp alert to the seller so they can follow up with the
// customer. Carries only what the customer told us plus the fixed product name:
// no price, and nothing about the product the bot was not given.
export async function notifySellerOfReferral(api, customerPhone, details, source) {
  try {
    const customer = normalizePhone(customerPhone || "");
    if (!customer) return;
    const body =
      `New customer lead from Genae 📩\n` +
      `Product: ${PRODUCT_NAME}\n` +
      `Customer: ${customer}\n` +
      `What they want: "${snippet(details)}"\n` +
      `Saw the advert: "${snippet(source)}"\n\n` +
      `Please contact them directly.`;
    await helperLimiter(() =>
      runTextCommand(
        "openclaw",
        ["message", "send", "--channel", "whatsapp", "--target", SELLER_WHATSAPP, "--message", body],
        { timeoutMs: HELPER_TIMEOUT_MS },
      ),
    );
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to notify seller of referral (${String(err)})`);
  }
}

// Entry point for menu option 5. Pins the sender to the referral flow and asks
// the first question; the answers are gathered by handleSellerReferral below.
export function startSellerReferral(api, sender) {
  saveServiceState(api, sender, SELLER_SERVICE, SELLER_STEP_DETAILS);
  return { handled: true, text: introText() };
}

// One user turn inside the referral flow. `state` is the sender's service-state
// slot, already known to belong to this flow.
export async function handleSellerReferral(api, sender, text = "", state = null) {
  const step = state?.step ?? SELLER_STEP_DETAILS;

  if (step === SELLER_STEP_SOURCE) {
    const details = typeof state?.data?.details === "string" ? state.data.details : "";
    await notifySellerOfReferral(api, sender, details, text);
    clearServiceState(api, sender);
    return { handled: true, text: HANDOFF_MESSAGE };
  }

  // First answer: store what the customer wants, then ask where they saw it.
  saveServiceState(api, sender, SELLER_SERVICE, SELLER_STEP_SOURCE, { details: snippet(text) });
  return { handled: true, text: SOURCE_QUESTION };
}
