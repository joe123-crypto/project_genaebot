import { expandHome, getPluginConfig, parseBoolean, readEnvValue } from "../lib/config.js";
import { getEventChannel, getEventSender, isWhatsAppChannel, normalizePhone } from "../lib/events.js";
import { HELPER_TIMEOUT_MS, helperLimiter, runJsonCommand, runTextCommand } from "../lib/runtime.js";

// The member store CLI still lives at the historical whatsapp-invite path; the
// invite-code commands are gone and only the phone-keyed member map remains.
const DEFAULT_MEMBER_STORE_SCRIPT = "~/.openclaw/workspace/bin/whatsapp-invite";
const DEFAULT_ALLOW_FAILED = `I couldn't unlock that number. Try again later.`;

// Keep admin alerts and stored snippets short and single-line.
const LEAD_SNIPPET_MAX = 280;
function leadSnippet(text) {
  return String(text ?? "").replace(/\s+/g, " ").trim().slice(0, LEAD_SNIPPET_MAX);
}

// Admin-only in-chat command to unlock a user, e.g. "allow +263788390903".
// "grandfather" is kept as a legacy spelling. Returns the normalized phone or "".
const ALLOW_COMMAND_PATTERN = /^(?:allow|grandfather)\s+(\d+@(?:lid|hosted\.lid)|\+?[\d][\d\s-]{6,})$/i;

export { DEFAULT_ALLOW_FAILED };

export function getMemberStoreScript(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.memberStoreScript === "string" && pluginCfg.memberStoreScript.trim()
      ? pluginCfg.memberStoreScript.trim()
      : DEFAULT_MEMBER_STORE_SCRIPT;
  return expandHome(configuredPath);
}

export function runMemberStoreScript(api, args) {
  const script = getMemberStoreScript(api);
  return helperLimiter(() => runJsonCommand(script, args, { timeoutMs: HELPER_TIMEOUT_MS }));
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

// Open-DM member mode is opt-in. When false, the gateway runs the classic
// allowlist and this plugin keeps its original member-only behavior.
export function memberGateEnabled(api) {
  const pluginCfg = getPluginConfig(api);
  return pluginCfg.memberGateEnabled === true;
}

export function parseAllowCommand(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 60) return "";
  const match = ALLOW_COMMAND_PATTERN.exec(compact);
  return match ? normalizePhone(match[1]) : "";
}

// Record a non-member's cold message as a pending lead. Returns the store result
// (notably `isNew`, false for repeat contacts) so the caller can avoid re-alerting.
export function recordLead(api, phone, text) {
  return runMemberStoreScript(api, ["request", "--phone", normalizePhone(phone || ""), "--message", leadSnippet(text)]);
}

// Best-effort WhatsApp alert to each configured admin when a new number first
// messages the bot, so they know a customer conversation has started. Alerts
// always originate from default, independently of the receiving/default account.
export async function notifyAdminOfLead(api, phone, text) {
  try {
    const target = normalizePhone(phone || "");
    if (!target) return;
    const admins = getAdminUsers(api);
    if (!admins.size) {
      api.logger?.warn?.("public-service-gate: lead recorded but no whatsappAdminUsers configured to notify");
      return;
    }
    const snippet = leadSnippet(text);
    const body = `New chat from ${target}:\n"${snippet}"`;
    for (const admin of admins) {
      await helperLimiter(() =>
        runTextCommand(
          "openclaw",
          ["message", "send", "--channel", "whatsapp", "--account", "default", "--target", admin, "--message", body],
          { timeoutMs: HELPER_TIMEOUT_MS },
        ),
      );
    }
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to notify admin of lead (${String(err)})`);
  }
}

// Best-effort WhatsApp alert to each admin when a DM user picks a service and says
// what they need, so the admin can follow up by hand. Includes the chosen service.
export async function notifyAdminOfIntake(api, phone, serviceLabel, text) {
  try {
    const target = normalizePhone(phone || "");
    if (!target) return;
    const admins = getAdminUsers(api);
    if (!admins.size) {
      api.logger?.warn?.("public-service-gate: intake captured but no whatsappAdminUsers configured to notify");
      return;
    }
    const snippet = leadSnippet(text);
    const body = `New service request from ${target}:\nService: ${serviceLabel}\n"${snippet}"`;
    for (const admin of admins) {
      await helperLimiter(() =>
        runTextCommand(
          "openclaw",
          ["message", "send", "--channel", "whatsapp", "--account", "default", "--target", admin, "--message", body],
          { timeoutMs: HELPER_TIMEOUT_MS },
        ),
      );
    }
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to notify admin of intake (${String(err)})`);
  }
}
