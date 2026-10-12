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
import { clearHandoff, isHandedOff } from "./lib/handoffs.js";
import { recordAdRefs } from "./lib/ad-refs.js";
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
  parseAllowCommand,
  runMemberStoreScript,
} from "./services/membership.js";
import { handleIntake } from "./services/intake.js";
import {
  HANDOFF_CATEGORIES,
  handleHandoff,
  loadBusinessInfo,
  parseResumeCommand,
} from "./services/handoff.js";
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
  startCvOrder,
} from "./services/cv-maker.js";

const PUBLIC_SERVICE_ACTIONS = [
  { name: "business_info", service: "core" },
  { name: "handoff", service: "core" },
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
    case "core":
      return true;
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

async function runPublicServiceAction(api, sender, action, text, params = {}) {
  const service = actionService(action);
  if (!service || !isServiceEnabled(api, service)) return toolText({ text: serviceDisabledReply(api) });
  switch (action) {
    case "business_info": return toolText({ text: loadBusinessInfo(api) });
    case "handoff": return toolText(await handleHandoff(api, sender, params.category, params.details || text));
    case "news_start_or_update": return toolText(await handleNewsRequest(api, sender, text));
    case "news_topics": return toolText(await handleNewsTopicsContinuation(api, sender, text));
    case "event_scout_start": return toolText(await handleEventScoutRequest(api, sender));
    case "job_scout_start_or_status": return toolText(await handleJobScoutRequest(api, sender, text));
    // Open-DM mode starts the automated CV order (the gate then relays each
    // interview turn); classic allowlist mode keeps the agent-driven intro.
    case "cv_maker_start":
      return toolText(
        memberGateEnabled(api) ? await startCvOrder(api, sender) : await handleCvMakerRequest(api, sender, text),
      );
    default: return toolText();
  }
}

function isEnabled(api) {
  const pluginCfg = getPluginConfig(api);
  return pluginCfg.enabled !== false;
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
          description: "Perform one supported Genaie public-service action for the authenticated WhatsApp sender: business_info returns the current products, prices and answering rules; handoff passes the customer to a person (send its reply text exactly); cv_maker_start starts the automated CV interview. Identity is bound by the runtime and cannot be supplied by the model.",
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
              category: {
                type: "string",
                enum: HANDOFF_CATEGORIES,
                description: "handoff only: what the customer wants.",
              },
              details: {
                type: "string",
                maxLength: 600,
                description: "handoff only: a short summary of the order or question (item, size, quantity, where to deliver, customer name if given).",
              },
            },
          },
          async execute(_id, params) {
            if (!authorized) return toolText();
            return runPublicServiceAction(api, sender, params.action, String(params.text || ""), {
              category: String(params.category || ""),
              details: String(params.details || ""),
            });
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

      // Observe-only: count promo ad ref codes ("ref BH-7K2") arriving in DMs so
      // ad/group performance can be measured. Never replies, claims, or throws,
      // so every flow below behaves exactly as before. Admin DMs are skipped so
      // operator testing doesn't inflate counts; group messages never get here
      // (claimed by the group hard block above).
      if (!admin) recordAdRefs(api, sender, text);
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

      // Admin-only: "resume +263..." lets the bot talk to a handed-off customer
      // again. Claimed here so the command never reaches the agent.
      if (admin && gateOn) {
        const resumeTarget = parseResumeCommand(text);
        if (resumeTarget) {
          let cleared = false;
          try {
            cleared = clearHandoff(api, resumeTarget);
          } catch (err) {
            api.logger?.warn?.(`public-service-gate: failed to clear handoff (${String(err)})`);
            return { handled: true, text: "That number couldn't be resumed. Try again later." };
          }
          return {
            handled: true,
            text: cleared ? `Resumed ${resumeTarget}: the bot will reply again.` : `${resumeTarget} was not handed off.`,
          };
        }
      }

      // Open-DM mode. Every DM reaches the public agent, which chats with the
      // customer and hands them off when they are ready to order. handleIntake
      // only holds back handed-off customers and relays the automated CV order.
      // Admins fall through to the main agent.
      if (gateOn) {
        if (admin) return;
        try {
          return await handleIntake(api, sender, text);
        } catch (err) {
          if (isQueueFullError(err)) return busyReply();
          api.logger?.warn?.(`public-service-gate: intake failed (${String(err)})`);
          return;
        }
      }

      // Classic allowlist mode (gate off): only configured public users reach the
      // agent; everyone else is left to core access control. Members must not have
      // their service UX short-circuited, so greetings, menu digits, and service
      // requests fall through to the agent (which has the public-service tools).
      const member = isWhatsAppPublicUser(api, event, ctx);
      if (!member) return; // leave to core access control
      // The handoff action is exposed in this mode too, so its silence must hold.
      if (isHandedOff(api, sender)) return { handled: true };
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
