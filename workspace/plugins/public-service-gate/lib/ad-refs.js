import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { getServiceStatePath } from "./state.js";

// ---------------------------------------------------------------------------
// Ad ref recorder
//
// The promo crons (workspace/lib/promo_broadcast.py) can embed a per-group ref
// code such as "BH-7K2" in a wa.me click-to-chat link whose prefilled text reads
// "Hi, I saw your borehole ad (ref BH-7K2)". When such a DM reaches the bot this
// module counts the ref so Joe can see which ads/groups drive replies. Decode a
// code to its promo + group with `post_ad.py --refs`.
//
// Observe-only: it never replies, never claims the message, and never throws, so
// the reply flow (intake, CV order, seller referral, agent) is unchanged.
//
// Only the promo prefixes below are matched (a bare "AB-123" in a message is not
// counted). Keep this list in sync with the TRACKING prefixes in
// workspace/cron/*-promo/post_*.py. The suffix uses promo_broadcast's REF_ALPHABET
// (Crockford base32: no I, L, O, U) and REF_CODE_LEN (3).
// ---------------------------------------------------------------------------

export const AD_REF_PREFIXES = ["BH", "BM", "BP", "CV", "FT", "GL", "GN", "IL", "PB", "SK"];
const AD_REF_PATTERN = new RegExp(
  `\\b(${AD_REF_PREFIXES.join("|")})-([0-9A-HJKMNP-TV-Z]{3})\\b`,
  "gi",
);
const AD_REFS_FILE = "ad-refs.json";

export function extractAdRefs(text) {
  const found = new Set();
  for (const match of String(text ?? "").matchAll(AD_REF_PATTERN)) {
    found.add(`${match[1].toUpperCase()}-${match[2].toUpperCase()}`);
  }
  return [...found];
}

// Stored next to the service state file (runtime/public-service-gate/), so a
// custom serviceStatePath (and every test's temp dir) carries it along.
export function getAdRefsPath(api) {
  return path.join(path.dirname(getServiceStatePath(api)), AD_REFS_FILE);
}

function readAdRefs(filePath) {
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
    const refs = parsed?.refs;
    return refs && typeof refs === "object" && !Array.isArray(refs) ? refs : {};
  } catch {
    return {};
  }
}

// Senders are stored only as a short one-way hash: enough to count distinct
// people per ref without adding phone numbers to this file.
function senderKey(sender) {
  return crypto.createHash("sha256").update(String(sender)).digest("hex").slice(0, 12);
}

// Record every ad ref in `text` from `sender`. Returns the refs recorded (empty
// when none). Best-effort: any I/O error is logged and swallowed.
export function recordAdRefs(api, sender, text, now = new Date()) {
  const refs = extractAdRefs(text);
  if (!refs.length) return [];
  try {
    const filePath = getAdRefsPath(api);
    const stored = readAdRefs(filePath);
    const at = now.toISOString();
    const who = sender ? senderKey(sender) : "";
    for (const ref of refs) {
      const entry = stored[ref] && typeof stored[ref] === "object" ? stored[ref] : {};
      const senders = Array.isArray(entry.senders) ? entry.senders : [];
      if (who && !senders.includes(who)) senders.push(who);
      stored[ref] = {
        count: (Number(entry.count) || 0) + 1,
        uniqueSenders: senders.length,
        firstSeen: entry.firstSeen || at,
        lastSeen: at,
        senders,
      };
    }
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    const tmpPath = path.join(path.dirname(filePath), `.${AD_REFS_FILE}.${process.pid}.tmp`);
    fs.writeFileSync(tmpPath, JSON.stringify({ version: 1, refs: stored }, null, 2) + "\n", {
      encoding: "utf8",
      mode: 0o600,
    });
    fs.renameSync(tmpPath, filePath);
    api.logger?.info?.(`public-service-gate: ad ref recorded ${refs.join(",")}`);
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to record ad ref (${String(err)})`);
  }
  return refs;
}
