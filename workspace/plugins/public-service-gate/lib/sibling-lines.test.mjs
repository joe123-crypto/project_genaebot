import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  classifySender,
  getSiblingLinesPath,
  lineKey,
  pairKey,
  recordAndCheck,
  resetSiblingGuardWarnings,
} from "./sibling-lines.js";

const DEFAULT_LINE = "+263785632943";
const ADS_LINE = "+263790453292";
const CUSTOMER = "+263771234567";

function apiWithLines(extraConfig = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sibling-lines-"));
  const warnings = [];
  const infos = [];
  resetSiblingGuardWarnings();
  return {
    dir,
    warnings,
    infos,
    api: {
      config: {
        plugins: {
          entries: {
            "public-service-gate": {
              config: {
                serviceStatePath: path.join(dir, "state.json"),
                whatsappLines: { default: DEFAULT_LINE, ads: ADS_LINE },
                ...extraConfig,
              },
            },
          },
        },
      },
      logger: {
        info(msg) { infos.push(msg); },
        warn(msg) { warnings.push(msg); },
        debug() {},
      },
    },
  };
}

const T0 = new Date("2026-10-21T09:00:00Z");
const at = (seconds) => new Date(T0.getTime() + seconds * 1000);

// --------------------------------------------------------------- classification

test("a line DMing the other line is a sibling DM", () => {
  const { api } = apiWithLines();
  // default's number arriving on the ads line
  const v = classifySender(api, { accountId: "ads", sender: DEFAULT_LINE });
  assert.equal(v.sibling, true);
  assert.equal(v.reason, "cross_line");
});

test("admin self-chat is never a sibling DM", () => {
  const { api } = apiWithLines();
  // the operator messaging the bot on its own number, on that same line
  const v = classifySender(api, { accountId: "default", sender: DEFAULT_LINE });
  assert.equal(v.sibling, false);
  assert.equal(v.reason, "self_chat");
});

test("a customer is never a sibling DM", () => {
  const { api } = apiWithLines();
  for (const accountId of ["default", "ads"]) {
    const v = classifySender(api, { accountId, sender: CUSTOMER });
    assert.equal(v.sibling, false, accountId);
    assert.equal(v.reason, "not_our_line", accountId);
  }
});

test("a bare JID-style number still matches a +E.164 config entry", () => {
  const { api } = apiWithLines();
  assert.equal(lineKey("263785632943"), lineKey(DEFAULT_LINE));
  const v = classifySender(api, { accountId: "ads", sender: "263785632943" });
  assert.equal(v.sibling, true);
});

test("an unknown receiving account fails open and warns once", () => {
  const { api, warnings } = apiWithLines();
  for (const accountId of ["", undefined, "nope"]) {
    const v = classifySender(api, { accountId, sender: DEFAULT_LINE });
    assert.equal(v.sibling, false, String(accountId));
    assert.equal(v.reason, "unknown_account", String(accountId));
  }
  assert.equal(warnings.length, 1, "warns once per process, not per message");
});

test("no configured lines, or a disabled guard, fails open", () => {
  const bare = apiWithLines({ whatsappLines: {} });
  assert.deepEqual(classifySender(bare.api, { accountId: "ads", sender: DEFAULT_LINE }), {
    sibling: false,
    reason: "no_lines_configured",
  });
  const off = apiWithLines({ siblingLineGuardEnabled: false });
  assert.equal(classifySender(off.api, { accountId: "ads", sender: DEFAULT_LINE }).reason, "guard_disabled");
});

// --------------------------------------------------------------------- budget

test("the pair may exchange the budget, then is held", () => {
  const { api } = apiWithLines();
  for (let i = 0; i < 8; i += 1) {
    const r = recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: `hi ${i}` }, at(i));
    assert.equal(r.suppressed, false, `message ${i + 1} should pass`);
    assert.equal(r.reason, "within_budget");
  }
  const over = recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "hi 9" }, at(9));
  assert.equal(over.suppressed, true);
  assert.equal(over.reason, "budget_exceeded");
  assert.equal(over.burstsToday, 1);
});

test("both directions share one budget", () => {
  const { api } = apiWithLines();
  // four each way is still eight for the pair
  for (let i = 0; i < 4; i += 1) {
    assert.equal(recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "a" }, at(i * 2)).suppressed, false);
    assert.equal(recordAndCheck(api, { accountId: "default", sender: ADS_LINE, text: "b" }, at(i * 2 + 1)).suppressed, false);
  }
  assert.equal(recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "c" }, at(20)).suppressed, true);
  assert.equal(pairKey(DEFAULT_LINE, ADS_LINE), pairKey(ADS_LINE, DEFAULT_LINE));
});

test("the cooldown holds the pair, then it may talk again", () => {
  const { api } = apiWithLines();
  for (let i = 0; i < 8; i += 1) recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "x" }, at(i));
  assert.equal(recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "x" }, at(9)).reason, "budget_exceeded");
  // still inside the 6 h cooldown
  const during = recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "x" }, at(3600));
  assert.equal(during.suppressed, true);
  assert.equal(during.reason, "cooldown");
  // after it expires
  const after = recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "x" }, at(6 * 3600 + 20));
  assert.equal(after.suppressed, false, "the pair may converse again after the cooldown");
});

test("the window slides, so slow chat is never held", () => {
  const { api } = apiWithLines();
  // one message every 10 minutes never fills a 15-minute window
  for (let i = 0; i < 30; i += 1) {
    const r = recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "slow" }, at(i * 600));
    assert.equal(r.suppressed, false, `message ${i + 1}`);
  }
});

test("the daily burst cap holds the pair until tomorrow", () => {
  const { api } = apiWithLines({ siblingLineCooldownSeconds: 60 });
  let t = 0;
  for (let burst = 1; burst <= 3; burst += 1) {
    for (let i = 0; i < 8; i += 1) recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "x" }, at(t++));
    const over = recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "x" }, at(t++));
    assert.equal(over.reason, "budget_exceeded");
    assert.equal(over.burstsToday, burst);
    t += 120; // ride out the short cooldown
  }
  // third burst exhausted the daily cap: held for the rest of the UTC day
  const sameDay = recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "x" }, at(12 * 3600));
  assert.equal(sameDay.suppressed, true);
  assert.equal(sameDay.reason, "cooldown");
  // next day the allowance returns
  const nextDay = recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "x" }, at(26 * 3600));
  assert.equal(nextDay.suppressed, false);
  assert.equal(nextDay.burstsToday, 0);
});

// ------------------------------------------------------------------ bookkeeping

test("the record keeps a readable transcript of the exchange", () => {
  const { api } = apiWithLines();
  recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "are you stuck?" }, at(0));
  recordAndCheck(api, { accountId: "default", sender: ADS_LINE, text: "Silent." }, at(1));

  const raw = fs.readFileSync(getSiblingLinesPath(api), "utf8");
  const { version, pairs } = JSON.parse(raw);
  assert.equal(version, 1);
  const entry = pairs[pairKey(DEFAULT_LINE, ADS_LINE)];
  assert.equal(entry.totalMessages, 2);
  assert.equal(entry.firstSeen, T0.toISOString());
  assert.deepEqual(
    entry.recent.map((r) => [r.from, r.text, r.suppressed]),
    [["default", "are you stuck?", false], ["ads", "Silent.", false]],
  );
});

test("a long message is truncated in the record", () => {
  const { api } = apiWithLines();
  recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "z".repeat(5000) }, at(0));
  const { pairs } = JSON.parse(fs.readFileSync(getSiblingLinesPath(api), "utf8"));
  const { text } = pairs[pairKey(DEFAULT_LINE, ADS_LINE)].recent[0];
  assert.ok(text.length < 250, `preview should be capped, got ${text.length}`);
});

test("bookkeeping failure holds the message instead of throwing", () => {
  const { api, warnings, dir } = apiWithLines();
  // a directory where the state file belongs makes the rename fail
  fs.mkdirSync(path.join(dir, "sibling-lines.json"));
  const r = recordAndCheck(api, { accountId: "ads", sender: DEFAULT_LINE, text: "x" }, at(0));
  assert.equal(r.suppressed, true, "fails closed: only ever silences bot-to-bot traffic");
  assert.equal(r.reason, "bookkeeping_failed");
  assert.equal(warnings.length, 1);
});

test("a non-sibling DM is not recorded at all", () => {
  const { api } = apiWithLines();
  const r = recordAndCheck(api, { accountId: "default", sender: CUSTOMER, text: "hi" }, at(0));
  assert.equal(r.suppressed, false);
  assert.equal(r.reason, "not_our_line");
  assert.equal(fs.existsSync(getSiblingLinesPath(api)), false);
});
