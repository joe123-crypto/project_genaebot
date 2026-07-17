import { getPluginConfig } from "./lib/config.js";
import {
  getEventChannel,
  getEventSender,
  getEventText,
  getOutboundRecipient,
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
import { isUnknownSenderRelevant } from "./lib/relevance.js";
import {
  getServiceMenuSelection,
  isGreetingOrServiceMenuRequest,
} from "./services/greeting.js";
import {
  DEFAULT_ALLOW_FAILED,
  isUnlockedMember,
  isWhatsAppAdmin,
  isWhatsAppPublicUser,
  loadAccessRequiredText,
  memberGateEnabled,
  memberStatus,
  notifyMemberAllowed,
  parseAllowCommand,
  runMemberStoreScript,
} from "./services/membership.js";
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

const PUBLIC_SERVICE_ACTIONS = [
  { name: "webetu_start", service: "webetu" },
  { name: "webetu_setup_done", service: "webetu" },
  { name: "webetu_restaurant_choice", service: "webetu" },
  { name: "webetu_reservation_status", service: "webetu" },
  { name: "news_start_or_update", service: "news" },
  { name: "news_topics", service: "news" },
  { name: "event_scout_start", service: "eventScout" },
  { name: "job_scout_start_or_status", service: "jobScout" },
];
const SERVICE_DISABLED_REPLY = "That isn't available here right now. This chat can help with Job Scout.";

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
  if (!service || !isServiceEnabled(api, service)) return toolText({ text: SERVICE_DISABLED_REPLY });
  switch (action) {
    case "webetu_start": return toolText(await handleWebetuOnboardingStart(api, sender));
    case "webetu_setup_done": return toolText(await handleWebetuSetupDone(api, sender));
    case "webetu_restaurant_choice": return toolText(await handleWebetuRestaurantChoice(api, sender, text));
    case "webetu_reservation_status": return toolText(await handleReservationStatus(api, sender));
    case "news_start_or_update": return toolText(await handleNewsRequest(api, sender, text));
    case "news_topics": return toolText(await handleNewsTopicsContinuation(api, sender, text));
    case "event_scout_start": return toolText(await handleEventScoutRequest(api, sender));
    case "job_scout_start_or_status": return toolText(await handleJobScoutRequest(api, sender, text));
    default: return toolText();
  }
}

function isEnabled(api) {
  const pluginCfg = getPluginConfig(api);
  return pluginCfg.enabled !== false;
}

function isKnownServiceRequestText(api, text) {
  return Boolean(
    getServiceMenuSelection(text) ||
      isGreetingOrServiceMenuRequest(text) ||
      (isServiceEnabled(api, "webetu") && (
        isWebetuOnboardingStartRequest(text) ||
        isWebetuGuideRequest(text) ||
        isWebetuSetupDoneRequest(text) ||
        isReservationStatusRequest(text)
      )) ||
      (isServiceEnabled(api, "news") && (isNewsStopRequest(text) || isNewsRequest(text))) ||
      (isServiceEnabled(api, "jobScout") && isJobScoutRequest(text)) ||
      (isServiceEnabled(api, "eventScout") && isEventScoutRequest(text)),
  );
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
  return Boolean(
    (!isServiceEnabled(api, "webetu") && (
      isWebetuRequestText(text) ||
      isWebetuOnboardingStartRequest(text) ||
      isWebetuGuideRequest(text) ||
      isWebetuSetupDoneRequest(text) ||
      isReservationStatusRequest(text)
    )) ||
      (!isServiceEnabled(api, "news") && (isNewsStopRequest(text) || isNewsRequest(text))) ||
      (!isServiceEnabled(api, "eventScout") && (isEventScoutRequest(text) || isCalendarSaveRequestText(text))),
  );
}

export default {
  id: "public-service-gate",
  name: "Public Service Gate",
  description: "Handles WhatsApp public-user safety and admin-managed member access control before agent dispatch.",
  version: "0.1.0",
  register(api) {
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

      // Admin-only unlock from chat: "allow +263...". Claimed here so the
      // command never reaches the agent or the relevance gate.
      if (admin && gateOn) {
        const allowTarget = parseAllowCommand(text);
        if (allowTarget) {
          try {
            const res = await runMemberStoreScript(api, ["grandfather", "--phone", allowTarget]);
            if (res.changed) await notifyMemberAllowed(api, allowTarget);
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

      const configuredMember = !gateOn && isWhatsAppPublicUser(api, event, ctx);
      const unlockedMember = gateOn && !admin && isUnlockedMember(await memberStatus(api, sender));
      const member = configuredMember || unlockedMember;

      // Membership gate. Only the admin can unlock a user; service-like messages
      // from everyone else get a brief access-required reply and unrelated
      // traffic stays silent. Admins fall through to the full agent.
      if (!member) {
        if (admin) return;
        if (!gateOn) return; // classic allowlist mode: leave to core access control
        if (isDisabledServiceRequestText(api, text)) {
          return { handled: true, text: SERVICE_DISABLED_REPLY };
        }
        if (await isUnknownSenderRelevant(api, text, {
          isKnownServiceRequest: (candidate) => isKnownServiceRequestText(api, candidate),
        })) {
          return { handled: true, text: loadAccessRequiredText(api) };
        }
        return { handled: true }; // silent drop unrelated traffic; claim it, no reply, no agent
      }

      // From here on, sender is a member. Do not short-circuit service UX:
      // greetings, menu digits, setup continuations, and service requests all
      // fall through to the agent, which has the public-service docs and tools.
      if (isDisabledServiceRequestText(api, text)) {
        return { handled: true, text: SERVICE_DISABLED_REPLY };
      }
      api.logger?.info?.(
        `public-service-gate: member WhatsApp text deferred to agent sender=${sender} textLen=${text.length}`,
      );
      return;
    });
  },
};

export { QueueFullError, createLimiter, isQueueFullError, runJsonCommand };
