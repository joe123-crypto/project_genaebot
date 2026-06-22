import { busyReply, isQueueFullError } from "../lib/runtime.js";
import {
  accountLinkInvite,
  accountLinkRequiredText,
  accountLinkStatus,
  scopedAppUrl,
} from "../lib/account-link.js";

const DEFAULT_EVENT_SCOUT_SETUP_FAILED = `I couldn't start event scouting setup right now. Please try again later.`;

const EVENT_SCOUT_REQUEST_PATTERNS = [
  /\bevent\s*(?:scout|scouting)\b/i,
  /\blocal\s+events?\b/i,
  /\b(?:find|look\s+for|search\s+for|scout|register|subscribe|sign\s*me\s*up|start|setup|set\s*up)\b.*\bevents?\b/i,
  /\bevents?\b.*\b(?:find|search|scout|register|subscribe|start|setup|set\s*up|calendar|remind(?:er|ers)?|near\s*me|in\s+my\s+area)\b/i,
];
// Explicit intent to set up / onboard event scouting. Only an event-scouting
// message that ALSO carries one of these keywords gets the canned setup link;
// every other event-related message (questions, "start/find/scout" phrasings,
// anything ambiguous) falls through to the relevance gate -> LLM, which decides
// how to respond from its system + public-service prompts.
const EVENT_SCOUT_SETUP_PATTERNS = [
  /\b(?:set\s*up|setup)\b/i,
  /\bonboard(?:ing|ed)?\b/i,
];

export function isEventScoutRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 420) return false;
  return EVENT_SCOUT_REQUEST_PATTERNS.some((pattern) => pattern.test(compact));
}

// Only an explicit "set up" / "onboard" event-scouting message is claimed
// deterministically. Anything else about events falls through to the LLM, so we
// no longer hijack questions like "can I change the events you scout?".
export function isEventScoutSetupCommand(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 420) return false;
  if (!EVENT_SCOUT_REQUEST_PATTERNS.some((pattern) => pattern.test(compact))) return false;
  return EVENT_SCOUT_SETUP_PATTERNS.some((pattern) => pattern.test(compact));
}

function eventScoutStartText(setupUrl) {
  return `Event scouting is active.

Open this setup link and sign in with your email:
${setupUrl}

After sign-in, connect Google and allow calendar access so I can add events to your calendar.

Then tell me what kinds of events you want (for example tech, business, or all), and I'll add matching upcoming events to your Google Calendar with reminders before each one.`;
}

export async function handleEventScoutRequest(api, sender) {
  try {
    const status = await accountLinkStatus(api, sender);
    if (!status.linked) {
      const invite = await accountLinkInvite(api, sender, "events", "/connect-gmail");
      if (!invite.setupUrl) throw new Error("missing setupUrl");
      return {
        handled: true,
        text: accountLinkRequiredText(invite.setupUrl, "Event scouting"),
      };
    }
    const setupUrl = scopedAppUrl(api, status.publicUserId, "/connect-gmail");
    return {
      handled: true,
      text: eventScoutStartText(setupUrl),
    };
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    api.logger?.warn?.(`public-service-gate: failed to start event scouting (${String(err)})`);
    return {
      handled: true,
      text: DEFAULT_EVENT_SCOUT_SETUP_FAILED,
    };
  }
}
