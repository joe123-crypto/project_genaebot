import fs from "node:fs";

import { expandHome, getPluginConfig, resolveAppPublicBaseUrl } from "./config.js";
import { normalizePhone, parseIsoTime } from "./events.js";
import { HELPER_TIMEOUT_MS, helperLimiter, runJsonCommand } from "./runtime.js";

const DEFAULT_ACCOUNT_LINK_SCRIPT = "~/.openclaw/workspace/bin/account-link";
const DEFAULT_PENDING_LINKS_PATH = "~/.openclaw/runtime/account-link/pending-links.json";

export function getAccountLinkScript(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.accountLinkScript === "string" && pluginCfg.accountLinkScript.trim()
      ? pluginCfg.accountLinkScript.trim()
      : DEFAULT_ACCOUNT_LINK_SCRIPT;
  return expandHome(configuredPath);
}

export function runAccountLinkScript(api, args) {
  const script = getAccountLinkScript(api);
  return helperLimiter(() => runJsonCommand(script, args, { timeoutMs: HELPER_TIMEOUT_MS }));
}

export function accountLinkStatus(api, phone) {
  return runAccountLinkScript(api, ["status", "--phone", phone]);
}

export function accountLinkInvite(api, phone, purpose, nextPath) {
  return runAccountLinkScript(api, [
    "create-invite",
    "--phone",
    phone,
    "--purpose",
    purpose,
    "--next-path",
    nextPath,
  ]);
}

export function accountLinkStartOnboarding(api, phone, service, channel) {
  return runAccountLinkScript(api, [
    "start-onboarding",
    "--phone",
    phone,
    "--service",
    service,
    "--channel",
    channel,
  ]);
}

export function getAgentGenaiePublicBaseUrl(api) {
  const pluginCfg = getPluginConfig(api);
  const configured =
    typeof pluginCfg.agentGenaiePublicBaseUrl === "string" && pluginCfg.agentGenaiePublicBaseUrl.trim()
      ? pluginCfg.agentGenaiePublicBaseUrl.trim()
      : resolveAppPublicBaseUrl();
  return configured.replace(/\/+$/, "");
}

export function scopedAppUrl(api, publicUserId, suffix = "") {
  const base = getAgentGenaiePublicBaseUrl(api);
  if (!publicUserId) return `${base}/login?next=${encodeURIComponent(suffix || "/")}`;
  return `${base}/${publicUserId}${suffix}`;
}

// These setup replies are returned to the LLM agent as tool results, which it then
// relays to the user. A sign-in setup URL carries a one-time `?token=...`, the
// user's own personal sign-in code, not a secret. Left to itself the model tends to
// "protect" it by masking the token (e.g. token=***), which silently breaks the
// link. Prefixing the tool result with an explicit directive keeps the agent from
// mangling the URL.
export const VERBATIM_LINK_DIRECTIVE =
  "[Relay the message below to the user exactly as written. The link is a one-time "
  + "personal sign-in URL, not a secret: include it in full with its token and never "
  + "mask, redact, shorten, or alter any part of it. Do not output token=***.]";

export function withVerbatimLinkDirective(message) {
  return `${VERBATIM_LINK_DIRECTIVE}\n\n${message}`;
}

export function accountLinkRequiredText(setupUrl, serviceName) {
  return withVerbatimLinkDirective(`Sign in here to set up ${serviceName}:
${setupUrl}

Check Spam if the sign-in email doesn't arrive.`);
}

export function getPendingLinksPath(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.pendingLinksPath === "string" && pluginCfg.pendingLinksPath.trim()
      ? pluginCfg.pendingLinksPath.trim()
      : DEFAULT_PENDING_LINKS_PATH;
  return expandHome(configuredPath);
}

export function readPendingLinks(api) {
  try {
    const raw = fs.readFileSync(getPendingLinksPath(api), "utf8");
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed;
    if (Array.isArray(parsed?.links)) return parsed.links;
  } catch (err) {
    api.logger?.debug?.(`public-service-gate: pending link cache unavailable (${String(err)})`);
  }
  return [];
}

export function hasPendingWebetuVaultLink(api, phone) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return false;
  const now = Date.now();
  return readPendingLinks(api).some((entry) => {
    if (!entry || typeof entry !== "object") return false;
    if (normalizePhone(entry.phone) !== normalizedPhone) return false;
    if (entry.service !== "account-link" || entry.purpose !== "webetu" || entry.nextPath !== "/vault") return false;
    if (!String(entry.setupUrl ?? "").trim()) return false;
    const expiresAt = parseIsoTime(entry.expiresAt);
    return Boolean(expiresAt && expiresAt.getTime() > now);
  });
}
