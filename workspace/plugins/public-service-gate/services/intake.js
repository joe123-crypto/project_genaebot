import { isQueueFullError } from "../lib/runtime.js";
import { isHandedOff } from "../lib/handoffs.js";
import { isTakenOverNow } from "../lib/takeover.js";
import { clearServiceState, loadServiceState } from "../lib/state.js";
import { isStateResetRequest } from "./greeting.js";
import { notifyAdminOfLead, recordLead } from "./membership.js";
import { CV_SERVICE, handleCvOrder } from "./cv-maker.js";

// Open-DM intake. Everyone who DMs gets a real conversation with the public
// agent, which answers from the business info page and hands customers off when
// they are ready to order (see services/handoff.js). This function only decides
// what must NOT reach the agent:
//   - a customer already handed off stays silent, so a person can take over;
//   - a customer inside the automated CV order is relayed to the CV maker.
// Everything else returns undefined and falls through to the agent.
export async function handleIntake(api, sender, text, accountId = "default") {
  // Defence in depth: index.js claims a taken-over chat at a higher priority
  // than this, so in practice nothing reaches here. Checked again above the
  // lead recording because a chat the operator is already in needs no lead and
  // no "new chat" alert, and because reply silence must not depend on hook
  // ordering staying as it is.
  if (isTakenOverNow(api, sender)) {
    api.logger?.info?.("public-service-gate: intake skipped, a person has this chat");
    return { handled: true };
  }

  // Record the contact as a lead; the admin is alerted once, on first contact.
  // Best effort: a store or messaging failure never blocks the reply.
  try {
    const res = await recordLead(api, sender, text);
    if (res?.isNew) await notifyAdminOfLead(api, sender, text);
  } catch (err) {
    if (!isQueueFullError(err)) {
      api.logger?.warn?.(`public-service-gate: failed to record lead (${String(err)})`);
    }
  }

  if (isHandedOff(api, sender)) {
    api.logger?.info?.("public-service-gate: handed-off customer message held silently");
    return { handled: true };
  }

  // The automated CV order is a multi-turn flow driven by the CV maker CLI, so
  // once a sender is pinned to it every turn is relayed there. "cancel" (see
  // isStateResetRequest) escapes back to the normal conversation.
  const state = loadServiceState(api, sender);
  if (state?.service === CV_SERVICE) {
    if ((state.data?.accountId || "default") !== accountId) return;
    if (!isStateResetRequest(text)) return handleCvOrder(api, sender, text, state, accountId);
    clearServiceState(api, sender);
  }
  return undefined;
}
