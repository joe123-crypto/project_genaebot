import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// Who the bot replies to, chosen on the console's Customer Bot page and written
// by deploy/railway/console_customer_bot.py (the shared disk contract):
//   everyone  every customer (the default, and what a missing file means)
//   ref       only customers who have sent a campaign ref code (lib/ad-refs.js)
//   off       no customer
// The operator's own number is never held back by any mode.
export const REPLY_MODES = ["off", "ref", "everyone"];
export const REPLY_MODE_PRIORITY = 105; // below takeover (110), above the failsafe stop and group block (100)
const FILE_LIMIT = 4 * 1024;

export function getReplyModePath() {
  const state = process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), ".openclaw");
  return path.join(state, "runtime", "customer-bot", "replies.json");
}

// Fails OPEN to "everyone": wrongly silencing every customer is worse than
// answering one the operator meant to hold. The console writes the file
// atomically, so an unreadable one means something else went wrong.
export function readReplyMode(api) {
  let raw;
  try {
    const fd = fs.openSync(getReplyModePath(), "r");
    try {
      const buffer = Buffer.alloc(FILE_LIMIT + 1);
      const length = fs.readSync(fd, buffer, 0, buffer.length, 0);
      if (length > FILE_LIMIT) throw new Error("reply mode file is too large");
      raw = buffer.subarray(0, length).toString("utf8");
    } finally {
      fs.closeSync(fd);
    }
  } catch (err) {
    if (err?.code === "ENOENT") return "everyone";
    api.logger?.warn?.(`public-service-gate: reply mode unreadable, replying to everyone (${String(err)})`);
    return "everyone";
  }
  try {
    const value = JSON.parse(raw);
    if (value?.version === 1 && REPLY_MODES.includes(value.mode)) return value.mode;
  } catch { /* reported below */ }
  api.logger?.warn?.("public-service-gate: reply mode invalid, replying to everyone");
  return "everyone";
}
