import { getPluginConfig } from "../lib/config.js";
import { saveServiceState } from "../lib/state.js";

const CV_MAKER_UNAVAILABLE = "The CV maker isn't available here right now. Try again later.";

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

export async function handleCvMakerRequest(api, sender, text = "") {
  if (getPluginConfig(api).cvMakerEnabled !== true) {
    return { handled: true, text: CV_MAKER_UNAVAILABLE };
  }
  // The deterministic entry point just offers the two paths. The agent, guided by
  // the cv-maker skill/doc, collects the CV (an uploaded PDF or a short question
  // set), runs `job-scout make-cv`, and that command renders and sends the
  // finished PDF back on WhatsApp. No account link, Gmail, or stored profile.
  try {
    saveServiceState(api, sender, "cv-maker", "cv-maker.awaiting_choice");
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to save cv-maker state (${String(err)})`);
  }
  return { handled: true, text: cvMakerIntro() };
}
