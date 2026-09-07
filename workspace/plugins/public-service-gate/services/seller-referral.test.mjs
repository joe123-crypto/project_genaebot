import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { loadServiceState } from "../lib/state.js";
import {
  PRODUCT_NAME,
  SELLER_SERVICE,
  SELLER_STEP_DETAILS,
  SELLER_STEP_SOURCE,
  SELLER_WHATSAPP,
  handleSellerReferral,
  isSellerReferralRequest,
  startSellerReferral,
} from "./seller-referral.js";

const USER = "+263700000001";

function apiFor(config = {}) {
  const serviceStatePath = path.join(
    fs.mkdtempSync(path.join(os.tmpdir(), "seller-referral-state-")),
    "state.json",
  );
  return {
    pluginConfig: { serviceStatePath, ...config },
    logger: { debug() {}, info() {}, warn() {} },
  };
}

// Runs fn with a fake `openclaw` binary first on PATH, so the best-effort seller
// WhatsApp send is captured instead of failing silently.
async function withFakeOpenclaw(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "seller-referral-openclaw-"));
  const log = path.join(dir, "openclaw.log");
  fs.writeFileSync(
    path.join(dir, "openclaw"),
    `#!/usr/bin/env bash\nprintf '%s\\n' "$*" >> ${JSON.stringify(log)}\n`,
    { mode: 0o755 },
  );
  const previous = process.env.PATH;
  process.env.PATH = `${dir}${path.delimiter}${previous}`;
  try {
    return await fn(() => (fs.existsSync(log) ? fs.readFileSync(log, "utf8") : ""));
  } finally {
    process.env.PATH = previous;
  }
}

test("isSellerReferralRequest matches advert phrasings and ignores unrelated text", () => {
  for (const text of [
    "quarry stones",
    "I saw your advert for stones",
    "how much are the stones you advertise",
    "I want to talk to the seller",
  ]) {
    assert.equal(isSellerReferralRequest(text), true, `expected match: ${text}`);
  }
  for (const text of ["make me a cv", "find me a job", "hello"]) {
    assert.equal(isSellerReferralRequest(text), false, `expected no match: ${text}`);
  }
});

test("startSellerReferral pins the sender to the flow and asks what they want", () => {
  const api = apiFor();
  const reply = startSellerReferral(api, USER);

  assert.equal(reply.handled, true);
  assert.match(reply.text, /quarry stones/i);
  assert.match(reply.text, /what exactly/i);
  const state = loadServiceState(api, USER);
  assert.equal(state.service, SELLER_SERVICE);
  assert.equal(state.step, SELLER_STEP_DETAILS);
});

test("the first answer is stored and the source question follows", async () => {
  const api = apiFor();
  startSellerReferral(api, USER);

  const reply = await handleSellerReferral(api, USER, "10 tonnes to Harare", loadServiceState(api, USER));

  assert.equal(reply.handled, true);
  assert.match(reply.text, /where did you see/i);
  const state = loadServiceState(api, USER);
  assert.equal(state.step, SELLER_STEP_SOURCE);
  assert.equal(state.data.details, "10 tonnes to Harare");
});

test("the source answer forwards the lead to the seller and confirms to the customer", async () => {
  await withFakeOpenclaw(async (sent) => {
    const api = apiFor();
    startSellerReferral(api, USER);
    await handleSellerReferral(api, USER, "10 tonnes to Harare", loadServiceState(api, USER));

    const reply = await handleSellerReferral(api, USER, "Facebook", loadServiceState(api, USER));

    assert.equal(reply.handled, true);
    assert.match(reply.text, /seller will contact you/i);

    const message = sent();
    // Delivered over WhatsApp to the WhatsApp-capable seller number only.
    assert.match(message, new RegExp(`--target \\${SELLER_WHATSAPP}`));
    assert.match(message, new RegExp(PRODUCT_NAME));
    assert.match(message, new RegExp(`Customer: \\${USER}`));
    assert.match(message, /10 tonnes to Harare/);
    assert.match(message, /Facebook/);
    // The referral must never carry a price: the bot does not know or invent one.
    assert.doesNotMatch(message, /\$|price|cost|US\$/i);

    // State is cleared once the referral is handed off.
    assert.equal(loadServiceState(api, USER), null);
  });
});
