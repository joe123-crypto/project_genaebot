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
import { clearServiceState, loadServiceState } from "./lib/state.js";
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
  isStateResetRequest,
  loadGreeting,
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
  DEFAULT_NEWS_STOPPED,
  handleNewsRequest,
  handleNewsTopicsContinuation,
  isNewsRequest,
  isNewsStopRequest,
  runNewsSubscriptionScript,
} from "./services/news.js";
import { handleJobScoutRequest, isJobScoutRequest } from "./services/job-scout.js";
import {
  handleEventScoutRequest,
  isEventScoutRequest,
  isEventScoutSetupCommand,
} from "./services/event-scout.js";

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

async function handleActiveServiceState(api, sender, text, state) {
  if (!state || typeof state !== "object") return undefined;

  if (state.step === "webetu.awaiting_restaurant" || state.step === "webetu.restaurant_not_found") {
    return handleWebetuRestaurantChoice(api, sender, text);
  }

  if (state.step === "news.awaiting_topics") {
    return await handleNewsTopicsContinuation(api, sender, text);
  }

  return undefined;
}

export default {
  id: "public-service-gate",
  name: "Public Service Gate",
  description: "Handles deterministic public WhatsApp greetings before agent dispatch.",
  version: "0.1.0",
  register(api) {
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
              return { handled: true, text: loadInvitePendingText(api) };
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

      // From here on, sender is a member: existing deterministic service handling.
      const activeState = loadServiceState(api, sender);
      if (activeState) {
        if (isStateResetRequest(text)) {
          clearServiceState(api, sender);
          return {
            handled: true,
            text: loadGreeting(api),
          };
        }
        const stateReply = await handleActiveServiceState(api, sender, text, activeState);
        if (stateReply) return stateReply;
        return;
      }

      const menuSelection = getServiceMenuSelection(text);

      if (menuSelection === "1" || isWebetuOnboardingStartRequest(text) || isWebetuGuideRequest(text)) {
        return await handleWebetuOnboardingStart(api, sender);
      }

      if (isWebetuSetupDoneRequest(text)) {
        return await handleWebetuSetupDone(api, sender);
      }

      if (isReservationStatusRequest(text)) {
        return await handleReservationStatus(api, sender);
      }

      if (menuSelection === "2") {
        return await handleNewsRequest(api, sender, "news");
      }

      if (menuSelection === "3") {
        return await handleEventScoutRequest(api, sender);
      }

      if (menuSelection === "4") {
        return await handleJobScoutRequest(api, sender);
      }

      if (isNewsStopRequest(text)) {
        try {
          await runNewsSubscriptionScript(api, ["disable", "--phone", sender]);
          clearServiceState(api, sender);
        } catch (err) {
          if (isQueueFullError(err)) return busyReply();
          api.logger?.warn?.(`public-service-gate: failed to disable news (${String(err)})`);
          return {
            handled: true,
            text: "I couldn't stop personalized news right now. Please try again later.",
          };
        }
        return {
          handled: true,
          text: DEFAULT_NEWS_STOPPED,
        };
      }

      if (isNewsRequest(text)) {
        return await handleNewsRequest(api, sender, text);
      }

      if (isJobScoutRequest(text)) {
        return await handleJobScoutRequest(api, sender);
      }

      if (isEventScoutSetupCommand(text)) {
        return await handleEventScoutRequest(api, sender);
      }

      if (isGreetingOrServiceMenuRequest(text)) {
        return {
          handled: true,
          text: loadGreeting(api),
        };
      }

      // No deterministic handler matched. Do NOT claim the message here — fall
      // through to the whatsapp-relevance-gate (priority -10), which is the
      // semantics allow/block gateway: on-topic text is passed to the agent,
      // off-topic text is rejected there with the same canned message.
      api.logger?.info?.(
        `public-service-gate: member WhatsApp text deferred to relevance gate sender=${sender} textLen=${text.length}`,
      );
      return;
    });
  },
};

export { QueueFullError, createLimiter, inviteLinkText, isQueueFullError, runJsonCommand };
