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
  DEFAULT_INVITE_FAILED,
  DEFAULT_INVITE_INVALID,
  inviteLinkText,
  inviteMembershipStatus,
  invitesEnabled,
  isInviteRequest,
  isPendingInviteMembership,
  isUnlockedInviteMembership,
  isWhatsAppAdmin,
  isWhatsAppPublicUser,
  loadInviteAwaitingText,
  loadInvitePendingText,
  loadInviteRequiredText,
  loadInviteWelcomeText,
  matchesSharedAck,
  notifyAdminOfJoin,
  notifyInviterUnlocked,
  parseGrandfatherCommand,
  parseJoinCode,
  runInviteScript,
} from "./services/invite.js";
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
  "webetu_start",
  "webetu_setup_done",
  "webetu_restaurant_choice",
  "webetu_reservation_status",
  "news_start_or_update",
  "news_topics",
  "event_scout_start",
  "job_scout_start_or_status",
];

function toolText(reply) {
  const text = typeof reply?.text === "string" ? reply.text.trim() : "";
  return { content: [{ type: "text", text: text || "I couldn't complete that request right now." }] };
}

async function runPublicServiceAction(api, sender, action, text) {
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

function isKnownServiceRequestText(text) {
  return Boolean(
    getServiceMenuSelection(text) ||
      isGreetingOrServiceMenuRequest(text) ||
      isInviteRequest(text) ||
      isWebetuOnboardingStartRequest(text) ||
      isWebetuGuideRequest(text) ||
      isWebetuSetupDoneRequest(text) ||
      isReservationStatusRequest(text) ||
      isNewsStopRequest(text) ||
      isNewsRequest(text) ||
      isJobScoutRequest(text) ||
      isEventScoutRequest(text),
  );
}

export default {
  id: "public-service-gate",
  name: "Public Service Gate",
  description: "Handles WhatsApp public-user safety and invite access control before agent dispatch.",
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
              action: { type: "string", enum: PUBLIC_SERVICE_ACTIONS },
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
      const openInvites = invitesEnabled(api);

      // Admin escape hatch: unlock a pre-invite user from chat. Claimed here so
      // the command never reaches the agent or the relevance gate.
      if (admin && openInvites) {
        const grandfatherTarget = parseGrandfatherCommand(text);
        if (grandfatherTarget) {
          try {
            const res = await runInviteScript(api, ["grandfather", "--phone", grandfatherTarget]);
            api.logger?.info?.("public-service-gate: admin grandfather handled deterministically");
            return {
              handled: true,
              text: `Grandfathered ${grandfatherTarget}: ${res.changed ? "unlocked" : "already a member"}.`,
            };
          } catch (err) {
            if (isQueueFullError(err)) return busyReply();
            api.logger?.warn?.(`public-service-gate: failed to grandfather (${String(err)})`);
            return { handled: true, text: DEFAULT_INVITE_FAILED };
          }
        }
      }

      const configuredMember = !openInvites && isWhatsAppPublicUser(api, event, ctx);
      let inviteMembership;
      async function getInviteMembership() {
        if (inviteMembership !== undefined) return inviteMembership;
        inviteMembership = openInvites && !admin
          ? await inviteMembershipStatus(api, sender)
          : { status: "unknown", pending: false, unlocked: false };
        return inviteMembership;
      }
      const currentInviteMembership = openInvites && !admin
        ? await getInviteMembership()
        : { status: "unknown", pending: false, unlocked: false };
      const pendingInviteMember = isPendingInviteMembership(currentInviteMembership);
      const unlockedInviteMember = isUnlockedInviteMembership(currentInviteMembership);
      const member = !pendingInviteMember && (configuredMember || unlockedInviteMember);

      // Invite issuance for existing users and pending activated users. Claimed
      // here, ahead of the relevance gate, so an "invite link" request is never
      // blocked as off-topic.
      if (openInvites && (admin || member || pendingInviteMember) && isInviteRequest(text)) {
        try {
          const invite = await runInviteScript(api, ["create", "--inviter", sender]);
          if (!invite.code) throw new Error("missing code");
          api.logger?.info?.("public-service-gate: invite link handled deterministically");
          return { handled: true, text: inviteLinkText(api, invite.code, { pending: pendingInviteMember }) };
        } catch (err) {
          if (isQueueFullError(err)) return busyReply();
          api.logger?.warn?.(`public-service-gate: failed to create invite (${String(err)})`);
          return { handled: true, text: DEFAULT_INVITE_FAILED };
        }
      }

      if (pendingInviteMember) {
        if (matchesSharedAck(text)) {
          return { handled: true, text: loadInviteAwaitingText(api) };
        }
        return { handled: true, text: loadInvitePendingText(api) };
      }

      // Membership gate. Non-members can redeem JOIN codes; service-like messages
      // get an invite-required reply; unrelated traffic stays silent. Admins fall
      // through to the full agent.
      if (!member) {
        if (admin) return;
        if (!openInvites) return; // classic allowlist mode: leave to core access control
        const code = parseJoinCode(text);
        if (code) {
          try {
            const res = await runInviteScript(api, ["redeem", "--code", code, "--phone", sender]);
            if (res.ok) {
              const inviter = normalizePhone(res.inviter || "");
              const inviterWasAdmin = isWhatsAppAdmin(api, inviter);
              await notifyAdminOfJoin(api, inviter, sender);
              if (inviter && res.inviterUnlocked && !inviterWasAdmin) {
                await notifyInviterUnlocked(api, inviter, sender);
              }
              api.logger?.info?.("public-service-gate: invite join handled deterministically");
              return { handled: true, text: loadInviteWelcomeText(api) };
            }
            return { handled: true, text: DEFAULT_INVITE_INVALID };
          } catch (err) {
            if (isQueueFullError(err)) return busyReply();
            api.logger?.warn?.(`public-service-gate: failed to redeem invite (${String(err)})`);
            return { handled: true, text: DEFAULT_INVITE_INVALID };
          }
        }
        if (await isUnknownSenderRelevant(api, text, { isKnownServiceRequest: isKnownServiceRequestText })) {
          return { handled: true, text: loadInviteRequiredText(api) };
        }
        return { handled: true }; // silent drop unrelated traffic; claim it, no reply, no agent
      }

      // From here on, sender is a member. Do not short-circuit service UX:
      // greetings, menu digits, setup continuations, and service requests all
      // fall through to the agent, which has the public-service docs and tools.
      api.logger?.info?.(
        `public-service-gate: member WhatsApp text deferred to agent sender=${sender} textLen=${text.length}`,
      );
      return;
    });
  },
};

export { QueueFullError, createLimiter, inviteLinkText, isQueueFullError, runJsonCommand };
