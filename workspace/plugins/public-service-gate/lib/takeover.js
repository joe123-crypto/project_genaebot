import fs from "node:fs";
import path from "node:path";

import { getPluginConfig, parseBoolean, readEnvValue } from "./config.js";
import { normalizePhone } from "./events.js";
import { getTakeoverClearedAt, wasTakeoverNotified } from "./handoffs.js";
import { getServiceStatePath } from "./state.js";
import { getWhatsAppLines, lineKey } from "./sibling-lines.js";

// ---------------------------------------------------------------------------
// Human takeover
//
// The operator runs the business from the same WhatsApp account as the bot, so
// a reply typed there by hand is outbound traffic, so no plugin hook
// ever sees it. The adapter does, one step before discarding it, which is where
// deploy/railway/whatsapp-human-takeover.mjs is injected: it writes the peer
// into takeovers.json beside the other per-peer stores.
//
// This module is the read side. A peer in that store is a chat a person is
// handling, so the bot says nothing there: its messages are claimed before the
// agent runs and any reply already on its way out is cancelled.
//
// Two files, one writer each, so there is no read-modify-write race whether or
// not the adapter and this plugin share a process: the detector only ever
// writes takeovers.json, this plugin only ever writes handoffs.json. "resume"
// therefore does not delete the detector's entry, it stamps a clear in
// handoffs.json and we compare the two timestamps. A later manual reply writes
// a newer entry and takes the chat over again, which is the behaviour we want.
// ---------------------------------------------------------------------------

export const TAKEOVER_ENV = "GENAIE_HUMAN_TAKEOVER";
const TAKEOVERS_FILE = "takeovers.json";
// Above FAILSAFE_STOP_PRIORITY: a chat a person is handling is decided before
// anything else, so a customer typing "stop" in it cannot clear their saved
// order on the way past.
export const TAKEOVER_PRIORITY = 110;

export function getTakeoversPath(api) {
  return path.join(path.dirname(getServiceStatePath(api)), TAKEOVERS_FILE);
}

export function isTakeoverEnabled() {
  return parseBoolean(readEnvValue(TAKEOVER_ENV)) !== false;
}

function readTakeovers(api) {
  try {
    const parsed = JSON.parse(fs.readFileSync(getTakeoversPath(api), "utf8"));
    const takeovers = parsed?.takeovers;
    return takeovers && typeof takeovers === "object" && !Array.isArray(takeovers) ? takeovers : {};
  } catch {
    return {};
  }
}

/** The peer's takeover record, or null. */
export function readTakeover(api, phone) {
  const key = normalizePhone(phone || "");
  if (!key) return null;
  const entry = readTakeovers(api)[key];
  return entry && typeof entry === "object" ? entry : null;
}

/**
 * Every spelling of a taken-over chat, so "resume +263..." releases the opaque
 * @lid entry for the same conversation too. Falls back to the key itself.
 */
export function takeoverPeers(api, phone) {
  const key = normalizePhone(phone || "");
  if (!key) return [];
  const peers = readTakeover(api, key)?.peers;
  const resolved = (Array.isArray(peers) ? peers : []).map((peer) => normalizePhone(peer)).filter(Boolean);
  return resolved.includes(key) ? resolved : [key, ...resolved];
}

/**
 * Numbers a takeover must never silence. The operator's own line is the command
 * channel, so an entry against it would swallow "resume" itself and leave no way
 * back.
 */
export function isTakeoverExempt(api, phone) {
  const key = normalizePhone(phone || "");
  if (!key) return true;
  // Read straight from config rather than through services/membership.js, so
  // this stays a lib module with no dependency on a service.
  const admins = getPluginConfig(api).whatsappAdminUsers;
  if (Array.isArray(admins) && admins.map((admin) => normalizePhone(admin)).includes(key)) return true;
  const digits = lineKey(key);
  if (!digits) return false;
  for (const line of getWhatsAppLines(api).values()) {
    if (line === digits) return true;
  }
  return false;
}

/** True when a person has taken this chat over and no later "resume" released it. */
export function isTakenOver(api, phone) {
  const key = normalizePhone(phone || "");
  if (!key) return false;
  const at = Date.parse(String(readTakeover(api, key)?.at ?? ""));
  if (!Number.isFinite(at)) return false;
  const cleared = getTakeoverClearedAt(api, key);
  return !Number.isFinite(cleared) || at > cleared;
}

/** The whole rule the hooks apply: the feature is on, and this peer is not exempt. */
export function isTakenOverNow(api, phone) {
  if (!isTakeoverEnabled()) return false;
  if (isTakeoverExempt(api, phone)) return false;
  return isTakenOver(api, phone);
}

/** When the detector last wrote, so a watcher can skip an unchanged store. */
export function takeoversChangedAt(api) {
  try {
    return fs.statSync(getTakeoversPath(api)).mtimeMs;
  } catch {
    return 0;
  }
}

/**
 * Chats a person has taken over that the operator has not been told about yet.
 * One entry per conversation: the detector records every spelling of a chat, so
 * the extra spellings are dropped here rather than announced twice.
 */
export function pendingTakeoverNotices(api, { limit = 20 } = {}) {
  if (!isTakeoverEnabled()) return [];
  const notices = [];
  const seen = new Set();
  for (const [key, entry] of Object.entries(readTakeovers(api))) {
    if (notices.length >= limit) break;
    const at = String(entry?.at ?? "");
    const peers = takeoverPeers(api, key);
    const conversation = peers.join("|");
    if (seen.has(conversation)) continue;
    seen.add(conversation);
    if (isTakeoverExempt(api, key)) continue;
    if (!isTakenOver(api, key)) continue;
    if (wasTakeoverNotified(api, key, at)) continue;
    notices.push({ peer: key, at });
  }
  return notices;
}
