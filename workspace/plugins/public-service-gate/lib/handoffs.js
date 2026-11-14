import fs from "node:fs";
import path from "node:path";

import { normalizePhone } from "./events.js";
import { getServiceStatePath } from "./state.js";

// ---------------------------------------------------------------------------
// Handoff store
//
// Once the public agent hands a customer off ("someone will contact you soon"),
// the bot stops replying to that customer for good: a person picks the chat up
// from there. This file is the durable record of who was handed off. Unlike the
// 30-minute service-state slot it never expires; only the admin "resume +<phone>"
// command clears an entry.
//
// Stored next to the service state file (runtime/public-service-gate/), so a
// custom serviceStatePath (and every test's temp dir) carries it along.
//
// The same file also carries this plugin's half of the human-takeover
// bookkeeping (see lib/takeover.js): when a chat was taken over by a person,
// "resume" cannot delete the detector's record, because that file has a single
// writer inside the WhatsApp adapter. It stamps a clear here instead, and the
// two timestamps are compared. Notification state lives here for the same
// reason: one writer per file, so no update can be lost.
// ---------------------------------------------------------------------------

const HANDOFFS_FILE = "handoffs.json";

export function getHandoffsPath(api) {
  return path.join(path.dirname(getServiceStatePath(api)), HANDOFFS_FILE);
}

function readStore(api) {
  try {
    const parsed = JSON.parse(fs.readFileSync(getHandoffsPath(api), "utf8"));
    const section = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
    return { handoffs: section(parsed?.handoffs), meta: section(parsed?.meta) };
  } catch {
    return { handoffs: {}, meta: {} };
  }
}

function readHandoffs(api) {
  return readStore(api).handoffs;
}

function writeStore(api, store) {
  const filePath = getHandoffsPath(api);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmpPath = path.join(path.dirname(filePath), `.${HANDOFFS_FILE}.${process.pid}.tmp`);
  fs.writeFileSync(tmpPath, JSON.stringify({ version: 1, ...store }, null, 2) + "\n", {
    encoding: "utf8",
    mode: 0o600,
  });
  fs.renameSync(tmpPath, filePath);
}

function writeHandoffs(api, handoffs) {
  writeStore(api, { handoffs, meta: readStore(api).meta });
}

function updateMeta(api, phone, fields) {
  const key = normalizePhone(phone || "");
  if (!key) return;
  const store = readStore(api);
  store.meta[key] = { ...(store.meta[key] && typeof store.meta[key] === "object" ? store.meta[key] : {}), ...fields };
  writeStore(api, store);
}

export function isHandedOff(api, phone) {
  const key = normalizePhone(phone || "");
  return Boolean(key && readHandoffs(api)[key]);
}

export function markHandedOff(api, phone, category, now = new Date()) {
  const key = normalizePhone(phone || "");
  if (!key) return;
  const handoffs = readHandoffs(api);
  handoffs[key] = { at: now.toISOString(), category: String(category || "other") };
  writeHandoffs(api, handoffs);
}

// Returns true when an entry was removed.
export function clearHandoff(api, phone) {
  const key = normalizePhone(phone || "");
  const store = readStore(api);
  if (!key || !store.handoffs[key]) return false;
  delete store.handoffs[key];
  writeStore(api, store);
  return true;
}

// ---------------------------------------------------------------------------
// Human-takeover bookkeeping. See lib/takeover.js for why it lives here.
// ---------------------------------------------------------------------------

/**
 * Record that the operator asked for a taken-over chat back. Stamped rather
 * than deleted, so a later manual reply (a newer record on the detector's side)
 * takes the chat over again.
 */
export function markTakeoverCleared(api, phone, now = new Date()) {
  updateMeta(api, phone, { takeoverClearedAt: now.toISOString() });
}

/** Milliseconds, or NaN when this chat was never resumed. */
export function getTakeoverClearedAt(api, phone) {
  const key = normalizePhone(phone || "");
  if (!key) return Number.NaN;
  return Date.parse(String(readStore(api).meta[key]?.takeoverClearedAt ?? ""));
}

/** The operator is told once per chat, not once per message. */
export function markTakeoverNotified(api, phone, at, now = new Date()) {
  updateMeta(api, phone, { takeoverNotifiedFor: String(at ?? ""), takeoverNotifiedAt: now.toISOString() });
}

export function wasTakeoverNotified(api, phone, at) {
  const key = normalizePhone(phone || "");
  if (!key) return true;
  return readStore(api).meta[key]?.takeoverNotifiedFor === String(at ?? "");
}
