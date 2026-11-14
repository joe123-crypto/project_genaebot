import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { markTakeoverCleared, markTakeoverNotified } from "./handoffs.js";
import {
  TAKEOVER_ENV,
  isTakenOver,
  isTakenOverNow,
  isTakeoverExempt,
  pendingTakeoverNotices,
  takeoverPeers,
  takeoversChangedAt,
} from "./takeover.js";

const CUSTOMER = "+263771234567";
const OTHER = "+263770000000";
const ADMIN = "+263785632943";
const ADS = "+263790453292";

function fixture(t, config = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "takeover-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const api = {
    config: {
      plugins: {
        entries: {
          "public-service-gate": {
            config: {
              serviceStatePath: path.join(dir, "state.json"),
              whatsappAdminUsers: [ADMIN],
              whatsappLines: { default: ADMIN, ads: ADS },
              ...config,
            },
          },
        },
      },
    },
    logger: { debug() {}, info() {}, warn() {} },
  };
  // Stands in for deploy/railway/whatsapp-human-takeover.mjs, which is the only
  // writer of this file in production.
  const detect = (takeovers) => fs.writeFileSync(path.join(dir, "takeovers.json"), JSON.stringify({ version: 1, takeovers }));
  const entry = (at, peers) => ({ at, accountId: "default", peers });
  return { dir, api, detect, entry };
}

async function withTakeoverEnv(value, fn) {
  const had = Object.prototype.hasOwnProperty.call(process.env, TAKEOVER_ENV);
  const previous = process.env[TAKEOVER_ENV];
  if (value === undefined) delete process.env[TAKEOVER_ENV];
  else process.env[TAKEOVER_ENV] = value;
  try {
    return await fn();
  } finally {
    if (had) process.env[TAKEOVER_ENV] = previous;
    else delete process.env[TAKEOVER_ENV];
  }
}

test("a chat with no record is the bot's to answer", (t) => {
  const f = fixture(t);
  assert.equal(isTakenOver(f.api, CUSTOMER), false);
  assert.equal(isTakenOverNow(f.api, CUSTOMER), false);
  assert.equal(isTakenOverNow(f.api, ""), false);
  assert.equal(takeoversChangedAt(f.api), 0);
});

test("a chat the detector recorded is a person's", (t) => {
  const f = fixture(t);
  f.detect({ [CUSTOMER]: f.entry("2026-11-14T08:00:00.000Z", [CUSTOMER]) });
  assert.equal(isTakenOver(f.api, CUSTOMER), true);
  assert.equal(isTakenOverNow(f.api, CUSTOMER), true);
  assert.equal(isTakenOverNow(f.api, OTHER), false);
  assert.ok(takeoversChangedAt(f.api) > 0);
});

test("resume releases the chat, and a later reply takes it over again", (t) => {
  const f = fixture(t);
  f.detect({ [CUSTOMER]: f.entry("2026-11-14T08:00:00.000Z", [CUSTOMER]) });
  markTakeoverCleared(f.api, CUSTOMER, new Date("2026-11-14T09:00:00.000Z"));
  assert.equal(isTakenOverNow(f.api, CUSTOMER), false);

  // The detector cannot be asked to delete its entry (it owns that file alone),
  // so a fresh takeover is simply a newer timestamp.
  f.detect({ [CUSTOMER]: f.entry("2026-11-14T10:00:00.000Z", [CUSTOMER]) });
  assert.equal(isTakenOverNow(f.api, CUSTOMER), true);
});

test("the operator's own numbers can never be silenced", (t) => {
  const f = fixture(t);
  f.detect({
    [ADMIN]: f.entry("2026-11-14T08:00:00.000Z", [ADMIN]),
    [ADS]: f.entry("2026-11-14T08:00:00.000Z", [ADS]),
  });
  // An entry here would swallow "resume" itself and leave no way back.
  for (const own of [ADMIN, ADS]) {
    assert.equal(isTakeoverExempt(f.api, own), true, own);
    assert.equal(isTakenOverNow(f.api, own), false, own);
  }
  assert.equal(isTakeoverExempt(f.api, CUSTOMER), false);
});

test("the kill switch puts the bot back in every chat", async (t) => {
  const f = fixture(t);
  f.detect({ [CUSTOMER]: f.entry("2026-11-14T08:00:00.000Z", [CUSTOMER]) });
  await withTakeoverEnv("0", () => {
    assert.equal(isTakenOverNow(f.api, CUSTOMER), false);
    // The record itself is untouched, so switching back on restores the silence.
    assert.equal(isTakenOver(f.api, CUSTOMER), true);
    assert.deepEqual(pendingTakeoverNotices(f.api), []);
  });
  assert.equal(isTakenOverNow(f.api, CUSTOMER), true);
});

test("every spelling of one chat is released together", (t) => {
  const f = fixture(t);
  const peers = [CUSTOMER, "4455@lid"];
  f.detect({
    [CUSTOMER]: f.entry("2026-11-14T08:00:00.000Z", peers),
    "4455@lid": f.entry("2026-11-14T08:00:00.000Z", peers),
  });
  assert.deepEqual(takeoverPeers(f.api, CUSTOMER), peers);
  assert.deepEqual(takeoverPeers(f.api, "4455@lid"), peers);
  // What index.js does for "resume +263771234567": without the opaque id being
  // stamped too, the bot would stay silent with no visible cause.
  for (const peer of takeoverPeers(f.api, CUSTOMER)) markTakeoverCleared(f.api, peer, new Date("2026-11-14T09:00:00.000Z"));
  assert.equal(isTakenOverNow(f.api, CUSTOMER), false);
  assert.equal(isTakenOverNow(f.api, "4455@lid"), false);
});

test("a peer with no record still names itself, so resume never throws", (t) => {
  const f = fixture(t);
  assert.deepEqual(takeoverPeers(f.api, CUSTOMER), [CUSTOMER]);
  assert.deepEqual(takeoverPeers(f.api, ""), []);
});

test("a store that is corrupt or half written leaves the bot talking", (t) => {
  const f = fixture(t);
  for (const body of ["{ not json", '{"version":1}', '{"version":1,"takeovers":[]}', '{"version":1,"takeovers":{"' + CUSTOMER + '":{"at":"soon"}}}']) {
    fs.writeFileSync(path.join(f.dir, "takeovers.json"), body);
    assert.equal(isTakenOverNow(f.api, CUSTOMER), false, body);
  }
});

test("the operator is owed one notice per chat, not per spelling or per message", (t) => {
  const f = fixture(t);
  const peers = [CUSTOMER, "4455@lid"];
  f.detect({
    [CUSTOMER]: f.entry("2026-11-14T08:00:00.000Z", peers),
    "4455@lid": f.entry("2026-11-14T08:00:00.000Z", peers),
    [OTHER]: f.entry("2026-11-14T08:05:00.000Z", [OTHER]),
    [ADMIN]: f.entry("2026-11-14T08:06:00.000Z", [ADMIN]),
  });
  assert.deepEqual(pendingTakeoverNotices(f.api).map((notice) => notice.peer), [CUSTOMER, OTHER]);

  markTakeoverNotified(f.api, CUSTOMER, "2026-11-14T08:00:00.000Z");
  assert.deepEqual(pendingTakeoverNotices(f.api).map((notice) => notice.peer), [OTHER]);

  // A chat taken over again after a resume is worth saying again.
  markTakeoverCleared(f.api, CUSTOMER, new Date("2026-11-14T09:00:00.000Z"));
  f.detect({ [CUSTOMER]: f.entry("2026-11-14T10:00:00.000Z", [CUSTOMER]) });
  assert.deepEqual(pendingTakeoverNotices(f.api), [{ peer: CUSTOMER, at: "2026-11-14T10:00:00.000Z" }]);

  assert.equal(pendingTakeoverNotices(f.api, { limit: 0 }).length, 0);
});
