import { expandHome, getPluginConfig, parseBoolean, readEnvValue } from "../lib/config.js";
import { getEventChannel, getEventSender, isWhatsAppChannel, normalizePhone } from "../lib/events.js";
import { HELPER_TIMEOUT_MS, helperLimiter, isQueueFullError, runJsonCommand, runTextCommand } from "../lib/runtime.js";

const DEFAULT_INVITE_SCRIPT = "~/.openclaw/workspace/bin/whatsapp-invite";
const DEFAULT_INVITE_FAILED = `I couldn't create an invite link right now. Please try again later.`;
const DEFAULT_INVITE_INVALID = `This invite link is invalid or has already been used. Ask your friend for a new one.`;
const DEFAULT_INVITE_REQUIRED = `This service is invite-only. Ask a friend who is already signed in and using Genaie Service Desk to send you their invite link. Open that link and send the pre-filled JOIN message here.`;
const DEFAULT_INVITE_WELCOME = `Welcome to Genaie Service Desk 🎉 A friend invited you, and your WhatsApp number is now activated.

We help you with:
1. Webetu Progress meal reservations
2. Personalized news delivered to your chat
3. Local event scouting around you
4. Job, scholarship, and internship scouting

One quick step before you can use it: just like a friend invited you, you invite one person too. Your account is not unlocked yet — it unlocks the moment the person you invite activates their number.

Ready? Type "invite link" and I'll send you a link to share with one person.`;
const DEFAULT_INVITE_PENDING = `You're almost in! Your account is not unlocked yet — one step unlocks it.

Type "invite link" to get your link, send it to one person, and your access unlocks the moment they activate their number.`;
const DEFAULT_INVITE_UNLOCKED = `Your invited person activated their WhatsApp number. Your access to Genaie Service Desk is now unlocked.`;
const DEFAULT_INVITE_AWAITING_ACTIVATION = `Got it — waiting for them to activate. Your access unlocks as soon as they do.`;

// An existing user (member or admin) asking for a link to invite someone else.
const INVITE_REQUEST_PATTERNS = [
  /\binvite\s+(?:link|code)\b/i,
  /\b(?:invite|refer)\s+(?:a\s+|my\s+|some\s+)?(?:friend|friends|someone|somebody|people|others?|buddy|buddies)\b/i,
  /\b(?:get|need|want|send|give|create|make|generate)\b[^.!?]*\binvite\b/i,
  /\b(?:how\s+(?:do|can)\s+i|can\s+i)\s+invite\b/i,
  /^invite(?:\s+me)?[\s!.?]*$/i,
];
// A pending user confirming they have shared their invite link, e.g. "Shared",
// "Sent it", "Done". Matched as a whole message so it doesn't fire on longer
// sentences that merely contain these words.
const INVITE_SHARED_KEYWORDS = [
  "shared",
  "shared it",
  "i shared",
  "i shared it",
  "sent",
  "sent it",
  "i sent",
  "i sent it",
  "forwarded",
  "i forwarded",
  "done",
];
// A new (not-yet-allowlisted) person sending the pre-filled wa.me join message,
// e.g. "JOIN-AB12CD34". Capture group 1 is the bare code.
const JOIN_CODE_PATTERN = /\bjoin[-\s]?([a-z0-9]{6,12})\b/i;

export { DEFAULT_INVITE_FAILED, DEFAULT_INVITE_INVALID };

export function getInviteScript(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.inviteScript === "string" && pluginCfg.inviteScript.trim()
      ? pluginCfg.inviteScript.trim()
      : DEFAULT_INVITE_SCRIPT;
  return expandHome(configuredPath);
}

export function runInviteScript(api, args) {
  const script = getInviteScript(api);
  return helperLimiter(() => runJsonCommand(script, args, { timeoutMs: HELPER_TIMEOUT_MS }));
}

export async function inviteMembershipStatus(api, phone) {
  try {
    return await runInviteScript(api, ["status", "--phone", phone]);
  } catch (err) {
    if (isQueueFullError(err)) throw err;
    api.logger?.warn?.(`public-service-gate: failed to check invite membership status (${String(err)})`);
    return { status: "unknown", pending: false, unlocked: false };
  }
}

export function isPendingInviteMembership(status) {
  return Boolean(status?.pending || status?.status === "pending");
}

export function isUnlockedInviteMembership(status) {
  return Boolean(status?.unlocked || status?.status === "unlocked");
}

export function getPublicUsers(api) {
  const pluginCfg = getPluginConfig(api);
  const explicit = Array.isArray(pluginCfg.publicUsers) ? pluginCfg.publicUsers : [];
  return new Set(explicit.map(normalizePhone).filter(Boolean));
}

export function isWhatsAppPublicUser(api, event, ctx) {
  if (!isWhatsAppChannel(getEventChannel(event, ctx))) return false;
  const sender = getEventSender(event, ctx);
  if (!sender) return false;
  return getPublicUsers(api).has(sender);
}

export function getAdminUsers(api) {
  const pluginCfg = getPluginConfig(api);
  const explicit = Array.isArray(pluginCfg.whatsappAdminUsers) ? pluginCfg.whatsappAdminUsers : [];
  return new Set(explicit.map(normalizePhone).filter(Boolean));
}

export function isAdminModeEnabled(api) {
  const envValue = parseBoolean(readEnvValue("WHATSAPP_ADMIN_ENABLED"));
  if (envValue !== undefined) return envValue;
  const pluginCfg = getPluginConfig(api);
  return pluginCfg.adminModeEnabled !== false;
}

export function isWhatsAppAdmin(api, sender) {
  if (!sender) return false;
  if (!isAdminModeEnabled(api)) return false;
  return getAdminUsers(api).has(sender);
}

// Digits-only bot number used to build wa.me links; empty string if unconfigured.
function getBotWhatsappNumber(api) {
  const pluginCfg = getPluginConfig(api);
  const raw = typeof pluginCfg.botWhatsappNumber === "string" ? pluginCfg.botWhatsappNumber : "";
  return raw.replace(/[^\d]/g, "");
}

// Peer invites + open DMs are opt-in. When false, the gateway runs the classic
// allowlist and this plugin keeps its original member-only behavior.
export function invitesEnabled(api) {
  const pluginCfg = getPluginConfig(api);
  return pluginCfg.invitesEnabled === true;
}

export function isInviteRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 200) return false;
  return INVITE_REQUEST_PATTERNS.some((pattern) => pattern.test(compact));
}

export function parseJoinCode(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 120) return "";
  const match = JOIN_CODE_PATTERN.exec(compact);
  return match ? match[1].toUpperCase() : "";
}

// Admin-only in-chat command to grandfather a pre-invite user, e.g.
// "grandfather +263788390903". Returns the normalized phone or "".
export function parseGrandfatherCommand(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 60) return "";
  const match = /^grandfather\s+(\+?[\d][\d\s-]{6,})$/i.exec(compact);
  return match ? normalizePhone(match[1]) : "";
}

export function matchesSharedAck(text) {
  const compact = String(text ?? "")
    .trim()
    .replace(/\s+/g, " ")
    .toLowerCase()
    .replace(/[\s!.?]+$/, "");
  if (!compact) return false;
  return INVITE_SHARED_KEYWORDS.includes(compact);
}

// One-line description of the service, reused in the forwardable invite block so
// the invited person knows what they're joining. Mirrors greeting.js and the
// relevance gate's serviceSummary.
const SERVICE_BLURB =
  "a WhatsApp assistant for Webetu Progress meal reservations, personalized news, local event scouting, and job/scholarship/internship scouting";

export function inviteLinkText(api, code, options = {}) {
  const botDigits = getBotWhatsappNumber(api);
  const joinText = `JOIN-${code}`;
  // The closing note depends on the inviter's own status: a pending inviter
  // unlocks their access when the invitee activates; an unlocked inviter does not.
  const closingNote = options.pending
    ? `This invite works once. When they activate, your own access unlocks.`
    : `This invite works once.`;

  if (!botDigits) {
    // Bot number not configured: hand over the code inside the forwardable block.
    return `Invite ONE person to Genaie Service Desk — just forward the whole message below to them 👇

———
Hi! I'm using Genaie Service Desk, ${SERVICE_BLURB}, and I'm inviting you to join.

To activate, message this number and send the code below exactly:

${joinText}

———

${closingNote}`;
  }

  const link = `https://wa.me/${botDigits}?text=${encodeURIComponent(joinText)}`;
  return `Invite ONE person to Genaie Service Desk — just forward the whole message below to them 👇

———
Hi! I'm using Genaie Service Desk, ${SERVICE_BLURB}, and I'm inviting you to join.

Tap this link and send the message that opens to activate your number:
${link}
———

${closingNote}`;
}

// Best-effort proactive notification to the admin when someone joins via invite.
export async function notifyAdminOfJoin(api, inviter, newMember) {
  try {
    const admin = [...getAdminUsers(api)][0];
    if (!admin) return;
    const msg =
      `New member joined via invite.\n` +
      `Invited by: ${inviter || "unknown"}\n` +
      `New member: ${newMember}`;
    await helperLimiter(() =>
      runTextCommand("openclaw", ["message", "send", "--channel", "whatsapp", "--target", admin, "--message", msg], {
        timeoutMs: HELPER_TIMEOUT_MS,
      }),
    );
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to notify admin of join (${String(err)})`);
  }
}

export async function notifyInviterUnlocked(api, inviter, newMember) {
  try {
    const target = normalizePhone(inviter || "");
    if (!target) return;
    const msg = `${loadInviteUnlockedText(api)}\n\nActivated by: ${newMember}`;
    await helperLimiter(() =>
      runTextCommand("openclaw", ["message", "send", "--channel", "whatsapp", "--target", target, "--message", msg], {
        timeoutMs: HELPER_TIMEOUT_MS,
      }),
    );
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to notify inviter unlock (${String(err)})`);
  }
}

export function loadInviteRequiredText(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.inviteRequiredText === "string" && pluginCfg.inviteRequiredText.trim()
    ? pluginCfg.inviteRequiredText.trim()
    : DEFAULT_INVITE_REQUIRED;
}

export function loadInviteWelcomeText(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.inviteWelcomeText === "string" && pluginCfg.inviteWelcomeText.trim()
    ? pluginCfg.inviteWelcomeText.trim()
    : DEFAULT_INVITE_WELCOME;
}

export function loadInvitePendingText(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.invitePendingText === "string" && pluginCfg.invitePendingText.trim()
    ? pluginCfg.invitePendingText.trim()
    : DEFAULT_INVITE_PENDING;
}

export function loadInviteUnlockedText(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.inviteUnlockedText === "string" && pluginCfg.inviteUnlockedText.trim()
    ? pluginCfg.inviteUnlockedText.trim()
    : DEFAULT_INVITE_UNLOCKED;
}

export function loadInviteAwaitingText(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.inviteAwaitingActivationText === "string" && pluginCfg.inviteAwaitingActivationText.trim()
    ? pluginCfg.inviteAwaitingActivationText.trim()
    : DEFAULT_INVITE_AWAITING_ACTIVATION;
}
