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
// ---------------------------------------------------------------------------

const HANDOFFS_FILE = "handoffs.json";

export function getHandoffsPath(api) {
  return path.join(path.dirname(getServiceStatePath(api)), HANDOFFS_FILE);
}

function readHandoffs(api) {
  try {
    const parsed = JSON.parse(fs.readFileSync(getHandoffsPath(api), "utf8"));
    const handoffs = parsed?.handoffs;
    return handoffs && typeof handoffs === "object" && !Array.isArray(handoffs) ? handoffs : {};
  } catch {
    return {};
  }
}

function writeHandoffs(api, handoffs) {
  const filePath = getHandoffsPath(api);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmpPath = path.join(path.dirname(filePath), `.${HANDOFFS_FILE}.${process.pid}.tmp`);
  fs.writeFileSync(tmpPath, JSON.stringify({ version: 1, handoffs }, null, 2) + "\n", {
    encoding: "utf8",
    mode: 0o600,
  });
  fs.renameSync(tmpPath, filePath);
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
  const handoffs = readHandoffs(api);
  if (!key || !handoffs[key]) return false;
  delete handoffs[key];
  writeHandoffs(api, handoffs);
  return true;
}
