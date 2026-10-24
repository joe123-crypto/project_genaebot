import fs from "node:fs";

import { expandHome, getPluginConfig } from "../lib/config.js";
import { normalizePhone } from "../lib/events.js";
import { markHandedOff } from "../lib/handoffs.js";
import { HELPER_TIMEOUT_MS, helperLimiter, runTextCommand } from "../lib/runtime.js";

// ---------------------------------------------------------------------------
// Handoff and business info (public agent tool actions)
//
// The public agent chats with customers using the facts in the business info
// page (docs/business-info.md). When a customer is ready to order, or asks
// something that page does not answer, the agent calls the `handoff` action:
// the customer is told someone will contact them, the request is forwarded to
// the partner that fulfils it (when there is one), and the bot then stays silent
// for that customer (see lib/handoffs.js) so a person can take over.
// ---------------------------------------------------------------------------

export const HANDOFF_CATEGORIES = [
  "fruit_trees",
  "building_materials",
  "borehole",
  "cv",
  "marketing",
  "other",
];

// Fruit trees and building materials are fulfilled by an outside partner reached
// on this WhatsApp line. Everything else is followed up by Genaie directly from
// this chat, so nothing is forwarded.
export const PARTNER_WHATSAPP = "+263775489417";
const FORWARDED_CATEGORIES = new Set(["fruit_trees", "building_materials"]);
const CATEGORY_LABELS = {
  fruit_trees: "Fruit trees",
  building_materials: "Building materials",
};

export const HANDOFF_REPLY = "Got it, your request is in. Someone will contact you soon.";

const DEFAULT_BUSINESS_INFO_PATH = "~/.openclaw/workspace/docs/business-info.md";
const BUSINESS_INFO_UNAVAILABLE =
  "Business info is unavailable right now. Do not quote any price or detail; use the handoff action instead.";

// Keep forwarded snippets bounded and single-line.
const DETAILS_MAX = 600;
function detailsSnippet(text) {
  return String(text ?? "").replace(/\s+/g, " ").trim().slice(0, DETAILS_MAX);
}

export function normalizeHandoffCategory(value) {
  const category = String(value ?? "").trim().toLowerCase();
  return HANDOFF_CATEGORIES.includes(category) ? category : "other";
}

export function loadBusinessInfo(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.businessInfoPath === "string" && pluginCfg.businessInfoPath.trim()
      ? pluginCfg.businessInfoPath.trim()
      : DEFAULT_BUSINESS_INFO_PATH;
  try {
    const text = fs.readFileSync(expandHome(configuredPath), "utf8").trim();
    return text || BUSINESS_INFO_UNAVAILABLE;
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to read business info (${String(err)})`);
    return BUSINESS_INFO_UNAVAILABLE;
  }
}

async function forwardToPartner(api, customer, category, details) {
  try {
    const body =
      `New customer request from Genaie\n` +
      `Product: ${CATEGORY_LABELS[category]}\n` +
      `Customer: ${customer}\n` +
      `Request: "${details}"\n\n` +
      `Please contact them directly.`;
    await helperLimiter(() =>
      runTextCommand(
        "openclaw",
        ["message", "send", "--channel", "whatsapp", "--target", PARTNER_WHATSAPP, "--message", body],
        { timeoutMs: HELPER_TIMEOUT_MS },
      ),
    );
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to forward handoff (${String(err)})`);
  }
}

// The `handoff` tool action. Forwarding is best effort; the customer is always
// handed off and told someone will be in touch.
export async function handleHandoff(api, sender, category, details) {
  const customer = normalizePhone(sender || "");
  const resolved = normalizeHandoffCategory(category);
  if (customer && FORWARDED_CATEGORIES.has(resolved)) {
    await forwardToPartner(api, customer, resolved, detailsSnippet(details));
  }
  try {
    markHandedOff(api, customer, resolved);
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to record handoff (${String(err)})`);
  }
  api.logger?.info?.(`public-service-gate: customer handed off category=${resolved}`);
  return { handled: true, text: HANDOFF_REPLY };
}

// Admin-only in-chat command to let the bot talk to a handed-off customer again,
// e.g. "resume +263771234567". Returns the normalized phone or "".
const RESUME_COMMAND_PATTERN = /^resume\s+(\+?[\d][\d\s-]{6,})$/i;

export function parseResumeCommand(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 60) return "";
  const match = RESUME_COMMAND_PATTERN.exec(compact);
  return match ? normalizePhone(match[1]) : "";
}
