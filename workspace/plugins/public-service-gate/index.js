import { getPluginConfig } from "./lib/config.js";
import {
  getEventChannel,
  getEventSender,
  getEventText,
  getOutboundRecipient,
  isGroupEvent,
  isWhatsAppChannel,
  normalizePhone,
} from "./lib/events.js";
import {
  QueueFullError,
  busyReply,
  createLimiter,
  isQueueFullError,
  runJsonCommand,
} from "./lib/runtime.js";
import { clearServiceState } from "./lib/state.js";
import {
  FAILSAFE_STOP_PRIORITY,
  clearFailsafeStopSuppression,
  hasFailsafeStopSuppression,
  isFailsafeStopCommand,
  markFailsafeStopSuppression,
} from "./lib/failsafe.js";
import {
  getServiceMenuSelection,
  isGreetingOrServiceMenuRequest,
} from "./services/greeting.js";
import {
  DEFAULT_ALLOW_FAILED,
  isWhatsAppAdmin,
  isWhatsAppPublicUser,
  memberGateEnabled,
  notifyMemberAllowed,
  parseAllowCommand,
  runMemberStoreScript,
} from "./services/membership.js";
import { handleIntake, primeIntakeSelection } from "./services/intake.js";
import {
  handleReservationStatus,
  handleWebetuOnboardingStart,
  handleWebetuRestaurantChoice,
  handleWebetuSetupDone,
  isReservationStatusRequest,
  isWebetuGuideRequest,
  isWebetuOnboardingStartRequest,
  isWebetuSetupDoneRequest,
} from "./services/webetu.js";
import {
  handleNewsRequest,
  handleNewsTopicsContinuation,
  isNewsRequest,
  isNewsStopRequest,
} from "./services/news.js";
import { handleJobScoutRequest, isJobScoutRequest } from "./services/job-scout.js";
import { handleEventScoutRequest, isEventScoutRequest } from "./services/event-scout.js";
import {
  handleCvDecision,
  handleCvMakerRequest,
  isCvMakerRequest,
  parseCvDecisionCommand,
} from "./services/cv-maker.js";

const PUBLIC_SERVICE_ACTIONS = [
  { name: "webetu_start", service: "webetu" },
  { name: "webetu_setup_done", service: "webetu" },
  { name: "webetu_restaurant_choice", service: "webetu" },
  { name: "webetu_reservation_status", service: "webetu" },
  { name: "news_start_or_update", service: "news" },
  { name: "news_topics", service: "news" },
  { name: "event_scout_start", service: "eventScout" },
  { name: "job_scout_start_or_status", service: "jobScout" },
  { name: "cv_maker_start", service: "cvMaker" },
];
const SERVICE_DISABLED_UNAVAILABLE = "That isn't available here right now.";

// The refusal names only the services actually switched on, so a chat with the
// CV maker disabled never offers a CV in the same breath as declining one.
function serviceDisabledReply(api) {
  const offers = [];
  if (isServiceEnabled(api, "jobScout")) offers.push("Job Scout");
  if (isServiceEnabled(api, "cvMaker")) offers.push("making a CV");
  if (!offers.length) return SERVICE_DISABLED_UNAVAILABLE;
  return `${SERVICE_DISABLED_UNAVAILABLE} This chat can help with ${offers.join(" or ")}.`;
}

function isServiceEnabled(api, service) {
  const pluginCfg = getPluginConfig(api);
  switch (service) {
    case "webetu":
      return pluginCfg.webetuEnabled !== false;
    case "news":
      return pluginCfg.newsEnabled !== false;
    case "eventScout":
      return pluginCfg.eventScoutEnabled !== false;
    case "jobScout":
      return pluginCfg.jobScoutOnboardingEnabled === true;
    case "cvMaker":
      return pluginCfg.cvMakerEnabled === true;
    default:
      return false;
  }
}

function publicServiceActions(api) {
  return PUBLIC_SERVICE_ACTIONS
    .filter((action) => isServiceEnabled(api, action.service))
    .map((action) => action.name);
}

function actionService(actionName) {
  return PUBLIC_SERVICE_ACTIONS.find((action) => action.name === actionName)?.service ?? "";
}

function toolText(reply) {
  const text = typeof reply?.text === "string" ? reply.text.trim() : "";
  return { content: [{ type: "text", text: text || "I couldn't complete that request right now." }] };
}

async function runPublicServiceAction(api, sender, action, text) {
  const service = actionService(action);
  if (!service || !isServiceEnabled(api, service)) return toolText({ text: serviceDisabledReply(api) });
  switch (action) {
    case "webetu_start": return toolText(await handleWebetuOnboardingStart(api, sender));
    case "webetu_setup_done": return toolText(await handleWebetuSetupDone(api, sender));
    case "webetu_restaurant_choice": return toolText(await handleWebetuRestaurantChoice(api, sender, text));
    case "webetu_reservation_status": return toolText(await handleReservationStatus(api, sender));
    case "news_start_or_update": return toolText(await handleNewsRequest(api, sender, text));
    case "news_topics": return toolText(await handleNewsTopicsContinuation(api, sender, text));
    case "event_scout_start": return toolText(await handleEventScoutRequest(api, sender));
    case "job_scout_start_or_status": return toolText(await handleJobScoutRequest(api, sender, text));
    case "cv_maker_start": return toolText(await handleCvMakerRequest(api, sender, text));
    default: return toolText();
  }
}

function isEnabled(api) {
  const pluginCfg = getPluginConfig(api);
  return pluginCfg.enabled !== false;
}

function isWebetuRequestText(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 360) return false;
  return /\b(?:webetu|meal|meals|reservation|reservations|restaurant)\b/i.test(compact);
}

function isCalendarSaveRequestText(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 420) return false;
  return /\b(?:calendar|google\s+calendar)\b/i.test(compact) &&
    /\b(?:add|save|put|schedule|remind|reminder)\b/i.test(compact);
}

function isDisabledServiceRequestText(api, text) {
  // A message that also matches an enabled signal (a Job Scout request, a
  // greeting, or a menu digit) must reach the agent: "any news on my job
  // applications?" is a Job Scout question, not a disabled-news request.
  if (getServiceMenuSelection(text) || isGreetingOrServiceMenuRequest(text)) return false;
  if (isServiceEnabled(api, "jobScout") && isJobScoutRequest(text)) return false;
  if (isServiceEnabled(api, "cvMaker") && isCvMakerRequest(text)) return false;
  return Boolean(
    (!isServiceEnabled(api, "webetu") && (
      isWebetuRequestText(text) ||
      isWebetuOnboardingStartRequest(text) ||
      isWebetuGuideRequest(text) ||
      isWebetuSetupDoneRequest(text) ||
      isReservationStatusRequest(text)
    )) ||
      (!isServiceEnabled(api, "news") && (isNewsStopRequest(text) || isNewsRequest(text))) ||
      (!isServiceEnabled(api, "eventScout") && (isEventScoutRequest(text) || isCalendarSaveRequestText(text))) ||
      (!isServiceEnabled(api, "cvMaker") && isCvMakerRequest(text)),
  );
}

export default {
  id: "public-service-gate",
  name: "Public Service Gate",
  description: "Handles WhatsApp public-user safety and admin-managed member access control before agent dispatch.",
  version: "0.1.0",
  register(api) {
    // Hard block: the bot never replies in group chats. Group messages are
    // claimed and dropped before any agent dispatch, so no AI reply is ever
    // generated — including when the bot is @-mentioned (the mention still lands
    // in a group, and this fires first). The rule is blanket: it applies to every
    // group, so any group the bot is later added to is covered automatically.
    // Intentionally NOT gated on isEnabled(api): flipping the plugin config off
    // must not re-open group replies. Proactive/CLI sends (e.g. promo broadcasts)
    // are unaffected — this only claims inbound messages before dispatch.
    api.on(
      "before_dispatch",
      async (event, ctx) => {
        const channel = getEventChannel(event, ctx);
        if (!isWhatsAppChannel(channel)) return;
        if (!isGroupEvent(event, ctx)) return;
        api.logger?.info?.(
          "public-service-gate: group message dropped before dispatch (groups are reply-disabled)",
        );
        return { handled: true }; // claim + no reply + no agent dispatch
      },
      { priority: FAILSAFE_STOP_PRIORITY },
    );

    api.registerTool(
      (ctx) => {
        if (ctx.agentId !== "public") return null;
        const sender = normalizePhone(ctx.requesterSenderId || "");
        const channel = String(ctx.messageChannel || ctx.deliveryContext?.channel || "").toLowerCase();
        const authorized = Boolean(sender && channel.includes("whatsapp"));
        return {
          name: "public_service",
          description: "Perform one supported Genaie public-service action for the authenticated WhatsApp sender. Identity is bound by the runtime and cannot be supplied by the model.",
          parameters: {
            type: "object",
            additionalProperties: false,
            required: ["action"],
            properties: {
              action: { type: "string", enum: publicServiceActions(api) },
              text: {
                type: "string",
                maxLength: 2000,
                description: "The user's current message. Include it so short onboarding choices such as 1, 2, web, or WhatsApp can continue the saved flow.",
              },
            },
          },
          async execute(_id, params) {
            if (!authorized) return toolText();
            return runPublicServiceAction(api, sender, params.action, String(params.text || ""));
          },
        };
      },
      { name: "public_service", optional: true },
    );

    api.on(
      "before_dispatch",
      async (event, ctx) => {
        if (!isEnabled(api)) return;
        const channel = getEventChannel(event, ctx);
        if (!isWhatsAppChannel(channel)) return;
        const text = getEventText(event);
        const sender = getEventSender(event, ctx);
        if (!isFailsafeStopCommand(text)) {
          if (sender && text) clearFailsafeStopSuppression(sender);
          return;
        }

        if (sender) {
          markFailsafeStopSuppression(sender);
          try {
            clearServiceState(api, sender);
          } catch (err) {
            api.logger?.warn?.(`public-service-gate: failed to clear state for failsafe stop (${String(err)})`);
          }
        } else {
          api.logger?.warn?.("public-service-gate: WhatsApp failsafe stop missing sender; handled fail-closed");
        }
        api.logger?.info?.("public-service-gate: failsafe stop handled before agent dispatch");
        return { handled: true };
      },
      { priority: FAILSAFE_STOP_PRIORITY },
    );

    api.on(
      "message_sending",
      async (event, ctx) => {
        if (!isEnabled(api)) return;
        const channel = getEventChannel(event, ctx);
        if (!isWhatsAppChannel(channel)) return;
        const recipient = getOutboundRecipient(event, ctx);
        if (!hasFailsafeStopSuppression(recipient)) return;

        api.logger?.info?.("public-service-gate: suppressed WhatsApp outbound after failsafe stop");
        return {
          cancel: true,
          cancelReason: "failsafe_stop",
          metadata: { suppressedBy: "public-service-gate", reason: "failsafe_stop" },
        };
      },
      { priority: FAILSAFE_STOP_PRIORITY },
    );

    api.on("before_dispatch", async (event, ctx) => {
      if (!isEnabled(api)) return;
      const channel = getEventChannel(event, ctx);
      if (!isWhatsAppChannel(channel)) return;
      const text = getEventText(event);
      const sender = getEventSender(event, ctx);
      if (!sender) {
        api.logger?.warn?.("public-service-gate: WhatsApp before_dispatch missing sender; handled fail-closed");
        return { handled: true };
      }

      const admin = isWhatsAppAdmin(api, sender);
      const gateOn = memberGateEnabled(api);

      // Admin-only CV order settlement from chat: "cv approve <id>" / "cv deny
      // <id>". This is the whole payment control, so it is gated on
      // isWhatsAppAdmin: for anyone else the text is never parsed as a command
      // and falls through to the normal DM handling below.
      if (admin && isServiceEnabled(api, "cvMaker")) {
        const decision = parseCvDecisionCommand(text);
        if (decision) {
          try {
            api.logger?.info?.(`public-service-gate: admin cv ${decision.action} handled deterministically`);
            return await handleCvDecision(api, decision);
          } catch (err) {
            if (isQueueFullError(err)) return busyReply();
            api.logger?.warn?.(`public-service-gate: failed to settle cv order (${String(err)})`);
            return { handled: true, text: "That CV order couldn't be updated. Try again later." };
          }
        }
      }

      // Admin-only unlock from chat: "allow +263...". Claimed here so the
      // command never reaches the agent or the relevance gate.
      if (admin && gateOn) {
        const allowTarget = parseAllowCommand(text);
        if (allowTarget) {
          try {
            const res = await runMemberStoreScript(api, ["grandfather", "--phone", allowTarget]);
            if (res.changed) {
              // Send the newly-approved user the menu, then prime their intake slot
              // so their next reply (a digit) is handled as a menu selection.
              await notifyMemberAllowed(api, allowTarget);
              primeIntakeSelection(api, allowTarget);
            }
            api.logger?.info?.("public-service-gate: admin allow handled deterministically");
            return {
              handled: true,
              text: `Allowed ${allowTarget}: ${res.changed ? "unlocked" : "already a member"}.`,
            };
          } catch (err) {
            if (isQueueFullError(err)) return busyReply();
            api.logger?.warn?.(`public-service-gate: failed to allow member (${String(err)})`);
            return { handled: true, text: DEFAULT_ALLOW_FAILED };
          }
        }
      }

      // Open-DM intake mode. Everyone who DMs gets the greeting + service menu,
      // has their request captured as a lead, and is handed off to the admin for
      // manual follow-up. No automated onboarding runs. Admins fall through to the
      // agent so they are never trapped in the intake flow.
      if (gateOn) {
        if (admin) return;
        try {
          return await handleIntake(api, sender, text);
        } catch (err) {
          if (isQueueFullError(err)) return busyReply();
          api.logger?.warn?.(`public-service-gate: intake failed (${String(err)})`);
          return { handled: true };
        }
      }

      // Classic allowlist mode (gate off): only configured public users reach the
      // agent; everyone else is left to core access control. Members must not have
      // their service UX short-circuited, so greetings, menu digits, and service
      // requests fall through to the agent (which has the public-service tools).
      const member = isWhatsAppPublicUser(api, event, ctx);
      if (!member) return; // leave to core access control
      if (isDisabledServiceRequestText(api, text)) {
        return { handled: true, text: serviceDisabledReply(api) };
      }
      api.logger?.info?.(
        `public-service-gate: member WhatsApp text deferred to agent sender=${sender} textLen=${text.length}`,
      );
      return;
    });
  },
};

export { QueueFullError, createLimiter, isQueueFullError, runJsonCommand };
