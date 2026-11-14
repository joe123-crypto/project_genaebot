import { getPluginConfig } from "./lib/config.js";
import { trackStandbyActivity } from "./lib/standby-activity.js";
import {
  getEventAccountId,
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
  clearHandoff,
  isHandedOff,
  markTakeoverCleared,
  markTakeoverNotified,
} from "./lib/handoffs.js";
import {
  TAKEOVER_PRIORITY,
  isTakenOverNow,
  pendingTakeoverNotices,
  takeoverPeers,
  takeoversChangedAt,
} from "./lib/takeover.js";
import { recordAdRefs } from "./lib/ad-refs.js";
import { BOREHOLE_SERVICES, lookupBorehole } from "./services/borehole.js";
import { recordAndCheck as checkSiblingLine } from "./lib/sibling-lines.js";
import { containsDash, stripDashes } from "./lib/style.js";
import { getReplyPacer, isReplyPacingEnabled } from "./lib/reply-pacing.js";
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
  notifyAdminOfTakeover,
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
  handleCvDecision,
  handleCvMakerRequest,
  isCvMakerRequest,
  parseCvDecisionCommand,
  startCvOrder,
} from "./services/cv-maker.js";

const PUBLIC_SERVICE_ACTIONS = [
  { name: "business_info", service: "core" },
  { name: "borehole_info", service: "core" },
  { name: "handoff", service: "core" },
  { name: "cv_maker_start", service: "cvMaker" },
];
const SERVICE_DISABLED_UNAVAILABLE = "That isn't available here right now.";
// Runs after every other outbound hook, so a cancelled reply is never held.
const REPLY_PACING_PRIORITY = -10;
// Above the longest hold (MAX_HOLD_MS in lib/reply-pacing.js): if the runner
// gives up first, the reply is sent at once rather than lost.
const REPLY_PACING_TIMEOUT_MS = 120_000;

// How often the operator's confirmation is looked for. The takeover itself needs
// no polling (both hooks read the store directly); this only exists so the
// operator hears about it even when the customer never writes again.
const TAKEOVER_NOTICE_INTERVAL_MS = 30_000;
let takeoverNoticeWatcher = null;

// Tell the operator, once per chat, that the bot has stepped out of it. Best
// effort throughout: a failed message must never make the bot speak again.
async function announceTakeovers(api) {
  for (const notice of pendingTakeoverNotices(api)) {
    try {
      await notifyAdminOfTakeover(api, notice.peer);
      markTakeoverNotified(api, notice.peer, notice.at);
      api.logger?.info?.("public-service-gate: operator told the bot stepped out of a chat");
    } catch (err) {
      api.logger?.warn?.(`public-service-gate: failed to announce a takeover (${String(err)})`);
      return;
    }
  }
}

// Armed once per process. A plugin reload must not stack intervals, and the
// timer must never hold the gateway open.
function watchTakeovers(api) {
  if (takeoverNoticeWatcher) return;
  let seenAt = takeoversChangedAt(api);
  takeoverNoticeWatcher = setInterval(() => {
    try {
      const changedAt = takeoversChangedAt(api);
      if (changedAt === seenAt) return;
      seenAt = changedAt;
      void announceTakeovers(api);
    } catch (err) {
      api.logger?.warn?.(`public-service-gate: takeover watch failed (${String(err)})`);
    }
  }, TAKEOVER_NOTICE_INTERVAL_MS);
  takeoverNoticeWatcher?.unref?.();
}

/** Test seam: the watcher is once per process, which would leak across tests. */
export function stopTakeoverWatch() {
  if (takeoverNoticeWatcher) clearInterval(takeoverNoticeWatcher);
  takeoverNoticeWatcher = null;
}

// The refusal names only the services actually switched on, so a chat with the
// CV maker disabled never offers a CV in the same breath as declining one.
function serviceDisabledReply(api) {
  const offers = [];
  if (isServiceEnabled(api, "cvMaker")) offers.push("making a CV");
  if (!offers.length) return SERVICE_DISABLED_UNAVAILABLE;
  return `${SERVICE_DISABLED_UNAVAILABLE} This chat can help with ${offers.join(" or ")}.`;
}

function isServiceEnabled(api, service) {
  const pluginCfg = getPluginConfig(api);
  switch (service) {
    case "core":
      return true;
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
    case "borehole_info": return toolText({ text: JSON.stringify(lookupBorehole(api, sender, params, params.accountId)) });
    case "handoff": return toolText(await handleHandoff(api, sender, params.category, params.details || text, params.accountId));
    // Open-DM mode starts the automated CV order (the gate then relays each
    // interview turn); classic allowlist mode keeps the agent-driven intro.
    case "cv_maker_start":
      return toolText(
        memberGateEnabled(api) ? await startCvOrder(api, sender, params.accountId) : await handleCvMakerRequest(api, sender, text),
      );
    default: return toolText();
  }
}

function isEnabled(api) {
  const pluginCfg = getPluginConfig(api);
  return pluginCfg.enabled !== false;
}

function isDisabledServiceRequestText(api, text) {
  // A message that also matches an enabled signal (a greeting or a menu digit)
  // must reach the agent. Any other off-topic ask
  // also reaches the agent, which answers from business-info.md; only an
  // explicit request for a switched-off service is refused here.
  if (getServiceMenuSelection(text) || isGreetingOrServiceMenuRequest(text)) return false;
  return Boolean(!isServiceEnabled(api, "cvMaker") && isCvMakerRequest(text));
}

export default {
  id: "public-service-gate",
  name: "Public Service Gate",
  description: "Handles WhatsApp public-user safety and admin-managed member access control before agent dispatch.",
  version: "0.1.0",
  register(api) {
    watchTakeovers(api);

    // A chat the operator answered by hand is theirs. The bot says nothing there
    // until they send "resume +<number>": the customer's messages are claimed
    // before the agent runs, and any reply already on its way out is cancelled
    // by the hook below. Detection happens inside the WhatsApp adapter, which
    // is the only place the operator's own outgoing message is visible; see
    // lib/takeover.js.
    //
    // Runs above FAILSAFE_STOP_PRIORITY so a taken-over customer typing "stop"
    // cannot clear their saved order on the way past, and intentionally NOT
    // gated on isEnabled(api): flipping the plugin config off must not put the
    // bot back into a conversation a person is having. GENAIE_HUMAN_TAKEOVER=0
    // is the switch for that.
    api.on(
      "before_dispatch",
      async (event, ctx) => {
        const channel = getEventChannel(event, ctx);
        if (!isWhatsAppChannel(channel)) return;
        if (isGroupEvent(event, ctx)) return; // groups are handled by the hard block below
        const sender = getEventSender(event, ctx);
        if (!isTakenOverNow(api, sender)) return;
        api.logger?.info?.("public-service-gate: message held silently, a person has this chat");
        await announceTakeovers(api);
        return { handled: true };
      },
      { priority: TAKEOVER_PRIORITY },
    );

    // The outbound half. This is what stops a reply the agent is still writing,
    // or one reply pacing is holding for up to MAX_HOLD_MS, from landing after
    // the operator has already answered. It keys on the takeover store only,
    // never on isHandedOff: handleHandoff marks the customer and THEN returns
    // "someone will contact you soon", so cancelling on a handoff would swallow
    // the agent's own confirmation.
    api.on(
      "message_sending",
      async (event, ctx) => {
        const channel = getEventChannel(event, ctx);
        if (!isWhatsAppChannel(channel)) return;
        // WhatsApp may address the same chat by a different id than the inbound
        // carried, so try every spelling, as reply pacing does.
        for (const recipient of [getOutboundRecipient(event, ctx), getOutboundRecipient(event), ctx?.senderId, ctx?.from, ctx?.conversationId]) {
          if (!isTakenOverNow(api, normalizePhone(String(recipient ?? "")))) continue;
          api.logger?.info?.(
            `public-service-gate: cancelled a WhatsApp reply, a person has this chat (len=${typeof event?.content === "string" ? event.content.length : 0})`,
          );
          void announceTakeovers(api);
          return {
            cancel: true,
            cancelReason: "human_takeover",
            metadata: { suppressedBy: "public-service-gate", reason: "human_takeover" },
          };
        }
      },
      { priority: TAKEOVER_PRIORITY },
    );

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
          description: "Perform one supported Genaie public-service action for the authenticated WhatsApp sender: business_info returns general business facts; borehole_info matches borehole offers to customer location and requirements and returns current prices; handoff passes the customer to a person (send its reply text exactly); cv_maker_start starts the automated CV interview. Identity is bound by the runtime and cannot be supplied by the model.",
          parameters: {
            type: "object",
            additionalProperties: false,
            required: ["action"],
            properties: {
              action: { type: "string", enum: publicServiceActions(api) },
              location: { type: "string", maxLength: 120, description: "borehole_info: customer's town or nearest major town, never an inferred location from an ad reference." },
              service: { type: "string", enum: BOREHOLE_SERVICES, description: "borehole_info: requested service. Use drilling_and_installation when both are requested so offers come from one supplier." },
              pump_hp: { type: "number", exclusiveMinimum: 0, maximum: 100, description: "borehole_info: requested installation pump power in HP, only if supplied by customer." },
              depth_m: { type: "number", exclusiveMinimum: 0, maximum: 2000, description: "borehole_info: requested drilling depth in meters." },
              casing_class: { type: "integer", minimum: 1, maximum: 100, description: "borehole_info: requested casing class." },
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
              ...Object.fromEntries(["location", "service", "pump_hp", "depth_m", "casing_class"].filter((k) => params[k] !== undefined).map((k) => [k, params[k]])),
              category: String(params.category || ""),
              details: String(params.details || ""),
              accountId: getEventAccountId({}, ctx) || "default",
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

    // Runs below the failsafe priority so a cancelled delivery stays cancelled:
    // this only rewrites a message that is already on its way out.
    api.on("message_sending", async (event, ctx) => {
      if (!isEnabled(api)) return;
      const channel = getEventChannel(event, ctx);
      if (!isWhatsAppChannel(channel)) return;
      const content = typeof event?.content === "string" ? event.content : "";
      if (!containsDash(content)) return;

      const rewritten = stripDashes(content);
      if (rewritten === content) return;
      api.logger?.info?.("public-service-gate: rewrote em dashes out of a WhatsApp reply");
      return { content: rewritten };
    });

    // Holds a customer reply for as long as a person would take to type it (see
    // lib/reply-pacing.js). Only a chat with a recent inbound on record is held,
    // so broadcasts, alerts and admin chats pass straight through. Returns
    // nothing: it never rewrites or cancels.
    api.on(
      "message_sending",
      async (event, ctx) => {
        if (!isEnabled(api) || !isReplyPacingEnabled()) return;
        const channel = getEventChannel(event, ctx);
        if (!isWhatsAppChannel(channel)) return;
        try {
          const accountId = getEventAccountId(event, ctx) || "default";
          const content = typeof event?.content === "string" ? event.content : "";
          // The reply target is usually the sender's phone, but WhatsApp may
          // address the same chat by a different id than the inbound carried.
          for (const recipient of [getOutboundRecipient(event), ctx?.senderId, ctx?.from, ctx?.conversationId]) {
            const held = await getReplyPacer().pauseBeforeSend(accountId, recipient, content);
            if (held === null) continue;
            api.logger?.info?.(`public-service-gate: paced WhatsApp reply held ${Math.round(held)}ms account=${accountId}`);
            break;
          }
        } catch (err) {
          api.logger?.warn?.(`public-service-gate: reply pacing failed open (${String(err)})`);
        }
      },
      { priority: REPLY_PACING_PRIORITY, timeoutMs: REPLY_PACING_TIMEOUT_MS },
    );

    api.on("before_dispatch", trackStandbyActivity(async (event, ctx) => {
      if (!isEnabled(api)) return;
      const channel = getEventChannel(event, ctx);
      if (!isWhatsAppChannel(channel)) return;
      const text = getEventText(event);
      const sender = getEventSender(event, ctx);
      if (!sender) {
        api.logger?.warn?.("public-service-gate: WhatsApp before_dispatch missing sender; handled fail-closed");
        return { handled: true };
      }

      // The bot's two WhatsApp lines are both auto-replying, so one DMing the
      // other loops forever (see lib/sibling-lines.js). They are allowed to
      // talk, but on a budget: under it the message falls through and the two
      // agents converse, over it we claim the message silently. This runs BEFORE
      // the admin check on purpose -- the admin number IS the "default" line's
      // number, so on the "ads" line a sibling DM would otherwise be read as an
      // operator command.
      const siblingLine = checkSiblingLine(api, { accountId: getEventAccountId(event, ctx), sender, text });
      if (siblingLine.suppressed) {
        api.logger?.info?.(
          `public-service-gate: sibling-line DM held (${siblingLine.reason}) pair=${siblingLine.pair ?? "?"} from=${siblingLine.from ?? "?"}`,
        );
        return { handled: true };
      }

      // A sibling line is never the operator: forcing admin off keeps one line
      // from running the other's admin commands (cv approve, allow, resume).
      // The admin number IS the "default" line's number, so without this the
      // "ads" line would read its sibling's chat as operator instructions.
      const admin = !siblingLine.sibling && isWhatsAppAdmin(api, sender);

      // Observe-only: count promo ad ref codes ("ref BH-7K2") arriving in DMs so
      // ad/group performance can be measured. Never replies, claims, or throws,
      // so every flow below behaves exactly as before. Admin DMs are skipped so
      // operator testing doesn't inflate counts; group messages never get here
      // (claimed by the group hard block above). A sibling line is not a
      // customer either, so its DMs are not counted.
      if (!admin && !siblingLine.sibling) recordAdRefs(api, sender, text);
      const gateOn = memberGateEnabled(api);

      // Only a customer's chat is paced: the operator and the sibling line get
      // no record, so their replies are never held.
      const accountId = getEventAccountId(event, ctx) || "default";
      const paced = !admin && !siblingLine.sibling && isReplyPacingEnabled();
      if (paced) getReplyPacer().noteInbound(accountId, sender, text);
      // Taken only when the message is about to reach the agent. A reply the
      // gate sends itself is held at send time instead.
      const pauseBeforeAgent = async () => {
        if (!paced) return;
        try {
          await getReplyPacer().pauseBeforeReply(accountId, sender);
        } catch (err) {
          api.logger?.warn?.(`public-service-gate: reply pacing failed open (${String(err)})`);
        }
      };

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
            // Both reasons the bot could be silent are released, and without
            // short-circuiting, so a leftover entry on either side can never
            // survive a resume. A chat taken over by hand is recorded under
            // every spelling WhatsApp used for it, so all of them are stamped.
            const peers = takeoverPeers(api, resumeTarget);
            const takenOver = peers.some((peer) => isTakenOverNow(api, peer));
            for (const peer of peers) markTakeoverCleared(api, peer);
            cleared = clearHandoff(api, resumeTarget) || takenOver;
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
        let claimed;
        try {
          claimed = await handleIntake(api, sender, text, accountId);
        } catch (err) {
          if (isQueueFullError(err)) return busyReply();
          api.logger?.warn?.(`public-service-gate: intake failed (${String(err)})`);
        }
        if (claimed === undefined) await pauseBeforeAgent();
        return claimed;
      }

      // Classic allowlist mode (gate off): only configured public users reach the
      // agent; everyone else is left to core access control. Members must not have
      // their service UX short-circuited, so greetings, menu digits, and service
      // requests fall through to the agent (which has the public-service tools).
      const member = isWhatsAppPublicUser(api, event, ctx);
      if (!member) return; // leave to core access control
      // The handoff action is exposed in this mode too, so its silence must hold.
      if (isHandedOff(api, sender) || isTakenOverNow(api, sender)) return { handled: true };
      if (isDisabledServiceRequestText(api, text)) {
        return { handled: true, text: serviceDisabledReply(api) };
      }
      api.logger?.info?.(
        `public-service-gate: member WhatsApp text deferred to agent sender=${sender} textLen=${text.length}`,
      );
      await pauseBeforeAgent();
      return;
    }));
  },
};

export { QueueFullError, createLimiter, isQueueFullError, runJsonCommand };
