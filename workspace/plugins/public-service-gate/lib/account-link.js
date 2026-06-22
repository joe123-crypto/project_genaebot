import fs from "node:fs";

import { expandHome, getPluginConfig, resolveAppPublicBaseUrl } from "./config.js";
import { normalizePhone, parseIsoTime } from "./events.js";
import { HELPER_TIMEOUT_MS, helperLimiter, runJsonCommand } from "./runtime.js";

const DEFAULT_ACCOUNT_LINK_SCRIPT = "~/.openclaw/workspace/bin/account-link";
const DEFAULT_PENDING_LINKS_PATH = "/home/joseph/.openclaw/runtime/account-link/pending-links.json";

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

export function accountLinkRequiredText(setupUrl, serviceName) {
  return `${serviceName} needs to link this WhatsApp chat to your app login first.

Open this link and sign in with your email:
${setupUrl}

If the sign-in email does not arrive, check your Spam or Junk folder.`;
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
