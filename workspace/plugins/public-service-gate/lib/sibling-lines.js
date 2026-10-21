import fs from "node:fs";
import path from "node:path";

import { getPluginConfig } from "./config.js";
import { getServiceStatePath } from "./state.js";

// ---------------------------------------------------------------------------
// Sibling-line guard
//
// The bot runs on two WhatsApp lines (channels.whatsapp.accounts: "default" and
// "ads"). Both auto-reply, so the moment one DMs the other they answer each
// other forever: on 2026-10-20 a single test message produced an endless
// "Silent." / "👌" exchange until one account was disabled. Nothing upstream
// stops it -- OpenClaw's pair loop protection needs a reliable inbound bot
// identity (allowBots), which a WhatsApp DM cannot provide, so WhatsApp is not a
// supported channel for it and `botLoopProtection` is rejected by the strict
// WhatsApp config schema. There is no deny list for WhatsApp DMs either, and an
// allowlist is unusable because an advertising line must accept DMs from
// strangers.
//
// The lines are ALLOWED to talk -- bounded. This module counts a cross-line DM
// against a sliding window and reports when the budget is spent, so the caller
// can claim the message silently. Option names mirror core's botLoopProtection
// (maxEventsPerWindow / windowSeconds / cooldownSeconds) but the implementation
// is self-contained: this plugin imports nothing from `openclaw`, and a failed
// module resolution would stop the plugin loading, which takes WhatsApp down.
// It is also file-backed rather than in-memory (core's guard is in-process) so a
// gateway restart cannot reset the budget and let a reconnect backlog restart
// the loop -- which is exactly how the incident would recur.
//
// The rule that makes this safe: suppress only when the sender is one of our own
// line numbers AND is not the number of the account that received it. The admin
// number IS the "default" line's number, so admin self-chat (sender == the
// receiving line's own number) must never be caught -- that is how the operator
// talks to the bot.
// ---------------------------------------------------------------------------

const SIBLING_LINES_FILE = "sibling-lines.json";

const DEFAULT_MAX_EVENTS_PER_WINDOW = 8;
const DEFAULT_WINDOW_SECONDS = 15 * 60;
const DEFAULT_COOLDOWN_SECONDS = 6 * 60 * 60;
const DEFAULT_MAX_BURSTS_PER_DAY = 3;

// Rolling transcript kept per pair so the operator can read what the two lines
// actually said to each other. Agent session transcripts reset daily at 04:00
// and prune at 30d, so this is the durable record.
const RECENT_LIMIT = 40;
const PREVIEW_LIMIT = 200;
// Pairs untouched for this long are dropped on the next write.
const PAIR_TTL_MS = 7 * 24 * 60 * 60 * 1000;

let warnedUnknownAccount = false;

// Digits-only key. normalizePhone() keeps a leading "+", so "+263790453292" and
// a bare JID-derived "263790453292" would otherwise be different keys (same
// reason lib/failsafe.js uses its own digits key).
export function lineKey(value) {
  return String(value ?? "").replace(/[^\d]/g, "");
}

// { accountId -> digits } from the whatsappLines config map.
export function getWhatsAppLines(api) {
  const raw = getPluginConfig(api).whatsappLines;
  const lines = new Map();
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    for (const [accountId, number] of Object.entries(raw)) {
      const id = String(accountId ?? "").trim();
      const digits = lineKey(number);
      if (id && digits) lines.set(id, digits);
    }
  }
  return lines;
}

export function isSiblingGuardEnabled(api) {
  return getPluginConfig(api).siblingLineGuardEnabled !== false;
}

function getSettings(api) {
  const cfg = getPluginConfig(api);
  const positiveInt = (value, fallback) =>
    Number.isInteger(value) && value >= 1 ? value : fallback;
  return {
    maxEventsPerWindow: positiveInt(cfg.siblingLineMaxEventsPerWindow, DEFAULT_MAX_EVENTS_PER_WINDOW),
    windowSeconds: positiveInt(cfg.siblingLineWindowSeconds, DEFAULT_WINDOW_SECONDS),
    cooldownSeconds: positiveInt(cfg.siblingLineCooldownSeconds, DEFAULT_COOLDOWN_SECONDS),
    maxBurstsPerDay: positiveInt(cfg.siblingLineMaxBurstsPerDay, DEFAULT_MAX_BURSTS_PER_DAY),
  };
}

// Is this DM one of our own lines talking to another of our own lines?
//
// Fails OPEN (sibling: false) whenever it cannot be sure -- guard disabled, no
// lines configured, no sender, or an unknown receiving account. Wrongly
// silencing a customer or the admin is worse than a loop, which can be stopped
// by hand; `reason` says which case it was.
export function classifySender(api, { accountId, sender } = {}) {
  if (!isSiblingGuardEnabled(api)) return { sibling: false, reason: "guard_disabled" };
  const lines = getWhatsAppLines(api);
  if (!lines.size) return { sibling: false, reason: "no_lines_configured" };
  const senderDigits = lineKey(sender);
  if (!senderDigits) return { sibling: false, reason: "no_sender" };
  if (![...lines.values()].includes(senderDigits)) {
    return { sibling: false, reason: "not_our_line" };
  }
  const account = String(accountId ?? "").trim();
  const ownNumber = account ? lines.get(account) : "";
  if (!ownNumber) {
    // The sender is one of our lines but we cannot tell which line received it,
    // so self-chat and cross-line are indistinguishable. Fail open and say so
    // once: without the account id this guard cannot work at all.
    if (!warnedUnknownAccount) {
      warnedUnknownAccount = true;
      api.logger?.warn?.(
        `public-service-gate: sibling-line guard inactive, unknown receiving account (accountId=${account || "missing"})`,
      );
    }
    return { sibling: false, reason: "unknown_account" };
  }
  // Admin self-chat: the operator messaging the bot on its own number.
  if (senderDigits === ownNumber) return { sibling: false, reason: "self_chat" };
  return { sibling: true, reason: "cross_line", accountId: account, ownNumber, senderLine: senderDigits };
}

// Stored next to the service state file (runtime/public-service-gate/), so a
// custom serviceStatePath (and every test's temp dir) carries it along.
export function getSiblingLinesPath(api) {
  return path.join(path.dirname(getServiceStatePath(api)), SIBLING_LINES_FILE);
}

function readPairs(filePath) {
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
    const pairs = parsed?.pairs;
    return pairs && typeof pairs === "object" && !Array.isArray(pairs) ? pairs : {};
  } catch {
    return {};
  }
}

function writePairs(filePath, pairs) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmpPath = path.join(path.dirname(filePath), `.${SIBLING_LINES_FILE}.${process.pid}.tmp`);
  fs.writeFileSync(tmpPath, JSON.stringify({ version: 1, pairs }, null, 2) + "\n", {
    encoding: "utf8",
    mode: 0o600,
  });
  fs.renameSync(tmpPath, filePath);
}

// Both directions of a pair share one budget.
export function pairKey(a, b) {
  return [lineKey(a), lineKey(b)].sort().join("~");
}

function prunePairs(pairs, nowMs) {
  const next = {};
  for (const [key, entry] of Object.entries(pairs || {})) {
    if (!entry || typeof entry !== "object") continue;
    const lastSeen = Date.parse(entry.lastSeen ?? "");
    if (Number.isFinite(lastSeen) && nowMs - lastSeen > PAIR_TTL_MS) continue;
    next[key] = entry;
  }
  return next;
}

function dayStamp(now) {
  return now.toISOString().slice(0, 10);
}

function endOfUtcDayMs(now) {
  return Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1, 0, 0, 0, 0);
}

function preview(text) {
  const flat = String(text ?? "").replace(/\s+/g, " ").trim();
  return flat.length > PREVIEW_LIMIT ? `${flat.slice(0, PREVIEW_LIMIT)}...` : flat;
}

// Count one inbound cross-line DM and decide whether the caller should claim it.
//
// Returns { suppressed, reason, ... }. `suppressed: false` with a non-"cross_line"
// reason means this was not a sibling-line DM at all and the caller should carry
// on exactly as before.
//
// Unlike classification, a bookkeeping failure fails CLOSED (suppressed: true):
// by this point the message is known to be one bot line talking to another, so
// suppressing can never silence a customer or the admin, while failing open
// would let a broken state file re-enable the loop.
export function recordAndCheck(api, { accountId, sender, text } = {}, now = new Date()) {
  const verdict = classifySender(api, { accountId, sender });
  if (!verdict.sibling) return { suppressed: false, ...verdict };

  const settings = getSettings(api);
  try {
    const filePath = getSiblingLinesPath(api);
    const nowMs = now.getTime();
    const at = now.toISOString();
    const key = pairKey(verdict.senderLine, verdict.ownNumber);
    const pairs = prunePairs(readPairs(filePath), nowMs);
    const stored = pairs[key] && typeof pairs[key] === "object" ? pairs[key] : {};

    const today = dayStamp(now);
    const sameDay = stored.day === today;
    const entry = {
      events: Array.isArray(stored.events) ? stored.events.filter((t) => Number.isFinite(t)) : [],
      cooldownUntil: Number.isFinite(stored.cooldownUntil) ? stored.cooldownUntil : 0,
      day: today,
      burstsToday: sameDay && Number.isInteger(stored.burstsToday) ? stored.burstsToday : 0,
      totalMessages: Number(stored.totalMessages) || 0,
      totalSuppressed: Number(stored.totalSuppressed) || 0,
      firstSeen: stored.firstSeen || at,
      lastSeen: at,
      recent: Array.isArray(stored.recent) ? stored.recent : [],
    };

    const from = verdict.accountId && getWhatsAppLines(api).get(verdict.accountId) === verdict.senderLine
      ? verdict.accountId
      : [...getWhatsAppLines(api).entries()].find(([, digits]) => digits === verdict.senderLine)?.[0]
        || verdict.senderLine;

    let suppressed = false;
    let reason = "within_budget";

    if (entry.cooldownUntil > nowMs) {
      suppressed = true;
      reason = "cooldown";
    } else {
      // Keep only the events still inside the window: a true sliding window.
      entry.events = entry.events.filter((t) => nowMs - t < settings.windowSeconds * 1000);
      if (entry.events.length >= settings.maxEventsPerWindow) {
        suppressed = true;
        reason = "budget_exceeded";
        entry.burstsToday += 1;
        // Out of bursts for today: stay quiet until tomorrow rather than
        // reopening after the normal cooldown.
        entry.cooldownUntil =
          entry.burstsToday >= settings.maxBurstsPerDay
            ? endOfUtcDayMs(now)
            : nowMs + settings.cooldownSeconds * 1000;
        entry.events = [];
      } else {
        entry.events.push(nowMs);
      }
    }

    if (suppressed) entry.totalSuppressed += 1;
    else entry.totalMessages += 1;

    entry.recent = [...entry.recent, { at, from, suppressed, text: preview(text) }].slice(-RECENT_LIMIT);

    pairs[key] = entry;
    writePairs(filePath, pairs);

    return {
      sibling: true,
      suppressed,
      reason,
      pair: key,
      from,
      remaining: Math.max(0, settings.maxEventsPerWindow - entry.events.length),
      cooldownUntil: entry.cooldownUntil ? new Date(entry.cooldownUntil).toISOString() : null,
      burstsToday: entry.burstsToday,
    };
  } catch (err) {
    api.logger?.warn?.(
      `public-service-gate: sibling-line bookkeeping failed, holding the message (${String(err)})`,
    );
    return { ...verdict, suppressed: true, reason: "bookkeeping_failed" };
  }
}

// Test seam: the unknown-account warning is emitted once per process.
export function resetSiblingGuardWarnings() {
  warnedUnknownAccount = false;
}
