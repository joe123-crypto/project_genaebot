import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { loadServiceState, saveServiceState } from "../lib/state.js";
import {
  CV_SERVICE,
  CV_STEP_APPROVAL,
  CV_STEP_INTERVIEW,
  CV_STEP_PAYMENT,
  cvPaymentText,
  handleCvDecision,
  handleCvMakerRequest,
  handleCvOrder,
  isCvMakerRequest,
  isCvPaidClaim,
  parseCvDecisionCommand,
  startCvOrder,
} from "./cv-maker.js";

const USER = "+263700000001";

function apiFor(config = {}) {
  const serviceStatePath = path.join(
    fs.mkdtempSync(path.join(os.tmpdir(), "cv-maker-state-")),
    "state.json",
  );
  return {
    pluginConfig: { cvMakerEnabled: true, serviceStatePath, ...config },
    logger: { debug() {}, info() {}, warn() {} },
  };
}

test("isCvMakerRequest matches CV creation and revamp phrasings", () => {
  for (const text of [
    "make me a cv",
    "can you create a professional CV for me",
    "I need my resume revamped",
    "please redo my curriculum vitae",
    "help me build a cv",
    "cv maker",
    "I want a resume written",
    "revamp my CV please",
  ]) {
    assert.equal(isCvMakerRequest(text), true, `expected to match: ${text}`);
  }
});

test("isCvMakerRequest ignores unrelated and job-application phrasings", () => {
  for (const text of [
    "",
    "hello there",
    "find me a job",
    "apply to jobs for me",
    "any scholarships near me",
    "what events are on this weekend",
  ]) {
    assert.equal(isCvMakerRequest(text), false, `expected not to match: ${text}`);
  }
});

test("isCvMakerRequest rejects oversized input", () => {
  assert.equal(isCvMakerRequest("make me a cv ".repeat(60)), false);
});

test("handleCvMakerRequest offers both paths when enabled", async () => {
  const api = apiFor();
  const reply = await handleCvMakerRequest(api, "+263700000001", "make me a cv");
  assert.equal(reply.handled, true);
  assert.match(reply.text, /one-page CV/i);
  assert.match(reply.text, /revamp/i);
  assert.match(reply.text, /from scratch/i);
});

// lib/state.js keeps one slot per phone, so writing cv-maker state would evict a
// Job Scout setup already in flight for the same sender.
test("handleCvMakerRequest does not touch shared service state", async () => {
  const api = apiFor();
  const statePath = api.pluginConfig.serviceStatePath;
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  const jobScoutState = {
    version: 1,
    states: {
      "+263700000001": {
        phone: "+263700000001",
        service: "job-scout",
        step: "job-scout.awaiting_setup_channel",
        data: {},
        updatedAt: new Date().toISOString(),
        expiresAt: new Date(Date.now() + 600000).toISOString(),
      },
    },
  };
  fs.writeFileSync(statePath, JSON.stringify(jobScoutState));

  await handleCvMakerRequest(api, "+263700000001", "make me a cv");

  const after = JSON.parse(fs.readFileSync(statePath, "utf8"));
  assert.equal(after.states["+263700000001"].service, "job-scout");
  assert.equal(after.states["+263700000001"].step, "job-scout.awaiting_setup_channel");
});

test("handleCvMakerRequest reports unavailable when disabled", async () => {
  const api = apiFor({ cvMakerEnabled: false });
  const reply = await handleCvMakerRequest(api, "+263700000001", "make me a cv");
  assert.equal(reply.handled, true);
  assert.match(reply.text, /isn't available here right now/i);
});

// ---------------------------------------------------------------------------
// Paid CV order flow
// ---------------------------------------------------------------------------

// A stand-in for bin/cv-maker: logs the argv it was given and prints the canned
// stdout for each subcommand. A string payload is emitted verbatim so the
// invalid-JSON path can be exercised too.
function writeCvScript(responses) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cv-maker-cli-"));
  const script = path.join(dir, "cv-maker");
  const log = path.join(dir, "calls.log");
  const lines = ["#!/usr/bin/env bash", "set -eu", `printf '%s\\n' "$*" >> ${JSON.stringify(log)}`, 'case "$1" in'];
  for (const [command, payload] of Object.entries(responses)) {
    const body = typeof payload === "string" ? payload : JSON.stringify(payload);
    lines.push(`  ${command})`, "cat <<'CVJSONEOF'", body, "CVJSONEOF", "    ;;");
  }
  lines.push(`  *) printf '%s\\n' '{"ok":false,"error":"unsupported command"}' ;;`, "esac", "");
  fs.writeFileSync(script, lines.join("\n"), { mode: 0o755 });
  return {
    script,
    calls: () => (fs.existsSync(log) ? fs.readFileSync(log, "utf8").trim().split("\n").filter(Boolean) : []),
  };
}

function cvApi(responses, config = {}) {
  const { script, calls } = writeCvScript(responses);
  const api = apiFor({ cvMakerScript: script, ...config });
  api.cvCalls = calls;
  return api;
}

function stepFor(api, phone = USER) {
  return loadServiceState(api, phone)?.step ?? null;
}

// Runs fn with a fake `openclaw` binary first on PATH, so the best-effort admin
// WhatsApp send in notifyAdminOfIntake is captured instead of failing silently.
async function withFakeOpenclaw(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cv-maker-openclaw-"));
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

test("startCvOrder pins the sender to the CV flow and introduces it", () => {
  const api = apiFor();
  const reply = startCvOrder(api, USER);

  assert.equal(reply.handled, true);
  assert.match(reply.text, /CV/);
  assert.doesNotMatch(reply.text, /—/);
  const state = loadServiceState(api, USER);
  assert.equal(state.service, CV_SERVICE);
  assert.equal(state.step, CV_STEP_INTERVIEW);
});

test("a CV flow turn is relayed to the CLI and answers with its reply", async () => {
  const api = cvApi({
    "interview-turn": { ok: true, orderId: "abc123", status: "interviewing", reply: "What is your full name?", complete: false },
  });
  startCvOrder(api, USER);

  const reply = await handleCvOrder(api, USER, "Ready", loadServiceState(api, USER));

  assert.equal(reply.handled, true);
  assert.equal(reply.text, "What is your full name?");
  assert.deepEqual(api.cvCalls(), [`interview-turn --phone ${USER} --message Ready`]);
  assert.equal(stepFor(api), CV_STEP_INTERVIEW);
});

test("awaiting_payment appends the configured price and payment details", async () => {
  const api = cvApi(
    {
      "interview-turn": { ok: true, orderId: "abc123", status: "awaiting_payment", reply: "Your CV is ready.", complete: true },
    },
    { cvMakerPriceText: "US$7", cvMakerPaymentDetails: "TestCash 0000 (Test Payee)" },
  );
  startCvOrder(api, USER);

  const reply = await handleCvOrder(api, USER, "done", loadServiceState(api, USER));

  assert.match(reply.text, /Your CV is ready\./);
  assert.match(reply.text, /US\$7/);
  assert.match(reply.text, /TestCash 0000 \(Test Payee\)/);
  assert.match(reply.text, /reply "paid"/i);
  assert.equal(stepFor(api), CV_STEP_PAYMENT);
});

test("cvPaymentText falls back to the shipped defaults", () => {
  const text = cvPaymentText(apiFor());
  assert.match(text, /US\$5/);
  assert.match(text, /EcoCash/);
});

test("chatter in the payment step re-sends the details without calling the CLI", async () => {
  const api = cvApi({ "interview-turn": { ok: true, orderId: "abc123", status: "interviewing", reply: "no" } });
  saveServiceState(api, USER, CV_SERVICE, CV_STEP_PAYMENT);

  const reply = await handleCvOrder(api, USER, "how much again?", loadServiceState(api, USER));

  assert.match(reply.text, /US\$5/);
  assert.deepEqual(api.cvCalls(), []);
  assert.equal(stepFor(api), CV_STEP_PAYMENT);
});

test('"paid" claims the payment, moves to approval, and alerts the admin', async () => {
  await withFakeOpenclaw(async (sent) => {
    const api = cvApi(
      { paid: { ok: true, orderId: "Ab3-_x", status: "awaiting_approval" } },
      { whatsappAdminUsers: ["+263700000009"] },
    );
    saveServiceState(api, USER, CV_SERVICE, CV_STEP_PAYMENT);

    const reply = await handleCvOrder(api, USER, "paid", loadServiceState(api, USER));

    assert.equal(reply.handled, true);
    assert.match(reply.text, /confirming your payment/i);
    assert.deepEqual(api.cvCalls(), [`paid --phone ${USER}`]);
    assert.equal(stepFor(api), CV_STEP_APPROVAL);

    const adminMessage = sent();
    assert.match(adminMessage, /--target \+263700000009/);
    assert.match(adminMessage, /Ab3-_x/);
    assert.match(adminMessage, /cv approve Ab3-_x/);
    assert.match(adminMessage, /cv deny Ab3-_x/);
  });
});

test("isCvPaidClaim only accepts a short unambiguous claim", () => {
  for (const text of ["paid", "Paid!", "i have paid", "I've paid", "payment sent", "money sent"]) {
    assert.equal(isCvPaidClaim(text), true, `expected paid claim: ${text}`);
  }
  for (const text of ["", "how do i pay", "i will pay tomorrow", "not paid", "paid?? what is the number for paying"]) {
    assert.equal(isCvPaidClaim(text), false, `expected not a paid claim: ${text}`);
  }
});

test("messages after payment is claimed get the review notice, not another CLI call", async () => {
  const api = cvApi({ "interview-turn": { ok: true, orderId: "abc123", status: "interviewing", reply: "no" } });
  saveServiceState(api, USER, CV_SERVICE, CV_STEP_APPROVAL);

  const reply = await handleCvOrder(api, USER, "any update?", loadServiceState(api, USER));

  assert.match(reply.text, /once it's approved/i);
  assert.deepEqual(api.cvCalls(), []);
});

test("a CLI ok:false error is shown as-is", async () => {
  const api = cvApi({ "interview-turn": { ok: false, error: "The CV interview is busy. Try again in a minute." } });
  startCvOrder(api, USER);

  const reply = await handleCvOrder(api, USER, "hello", loadServiceState(api, USER));

  assert.equal(reply.text, "The CV interview is busy. Try again in a minute.");
});

test("a crashing CLI produces a friendly message and never leaks stderr", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cv-maker-cli-"));
  const script = path.join(dir, "cv-maker");
  fs.writeFileSync(script, "#!/usr/bin/env bash\necho '/home/secret/order.pdf boom' >&2\nexit 3\n", { mode: 0o755 });
  const warnings = [];
  const api = apiFor({ cvMakerScript: script });
  api.logger.warn = (line) => warnings.push(line);
  startCvOrder(api, USER);

  const reply = await handleCvOrder(api, USER, "hello", loadServiceState(api, USER));

  assert.equal(reply.handled, true);
  assert.equal(reply.text, "I couldn't reach the CV maker. Try again later.");
  assert.doesNotMatch(reply.text, /secret|boom/);
  assert.equal(warnings.length, 1);
  assert.doesNotMatch(warnings[0], new RegExp(USER.replace("+", "\\+")));
});

test("a full helper queue answers with the busy reply", async () => {
  const api = cvApi({ "interview-turn": { ok: true, orderId: "abc123", status: "interviewing", reply: "hi" } });
  startCvOrder(api, USER);
  const state = loadServiceState(api, USER);

  // helperLimiter is maxConcurrent 4 / maxQueue 40, and every call reaches the
  // limiter synchronously, so the 45th is rejected before any can drain.
  const pending = [];
  for (let i = 0; i < 44; i += 1) pending.push(handleCvOrder(api, USER, "hi", state));
  const reply = await handleCvOrder(api, USER, "hi", state);

  assert.equal(reply.handled, true);
  assert.equal(reply.text, "I'm busy right now. Try again shortly.");
  await Promise.allSettled(pending);
});

test("handleCvOrder clears the flow when the CV maker is switched off", async () => {
  const api = cvApi({ "interview-turn": { ok: true, orderId: "abc123", status: "interviewing", reply: "hi" } }, { cvMakerEnabled: false });
  saveServiceState(api, USER, CV_SERVICE, CV_STEP_INTERVIEW);

  const reply = await handleCvOrder(api, USER, "hello", loadServiceState(api, USER));

  assert.match(reply.text, /isn't available here right now/i);
  assert.equal(loadServiceState(api, USER), null);
  assert.deepEqual(api.cvCalls(), []);
});

test("parseCvDecisionCommand matches only the exact admin command shape", () => {
  assert.deepEqual(parseCvDecisionCommand("cv approve Ab3-_x"), { action: "approve", orderId: "Ab3-_x" });
  assert.deepEqual(parseCvDecisionCommand("CV Deny  abc123"), { action: "deny", orderId: "abc123" });
  for (const text of [
    "",
    "cv approve",
    "cv approve abc",
    "cv approve abc123456789012",
    "cv approve abc123 please",
    "please cv approve abc123",
    "cv approve abc 123",
    "cv approve abc12$",
    "approve abc123",
    "cv revamp abc123",
  ]) {
    assert.equal(parseCvDecisionCommand(text), null, `expected no match: ${text}`);
  }
});

test("handleCvDecision approves and denies through the CLI", async () => {
  const approveApi = cvApi({ approve: { ok: true, orderId: "abc123", status: "sent", delivered: true, filename: "cv.pdf" } });
  const approved = await handleCvDecision(approveApi, { action: "approve", orderId: "abc123" });
  assert.equal(approved.handled, true);
  assert.match(approved.text, /approved and sent/i);
  assert.deepEqual(approveApi.cvCalls(), ["approve --order=abc123"]);

  const denyApi = cvApi({ deny: { ok: true, orderId: "abc123", status: "rejected" } });
  const denied = await handleCvDecision(denyApi, { action: "deny", orderId: "abc123" });
  assert.match(denied.text, /denied/i);
  assert.deepEqual(denyApi.cvCalls(), ["deny --order=abc123"]);
});

test("handleCvDecision reports an undelivered approval and CLI errors", async () => {
  const undelivered = cvApi({ approve: { ok: true, orderId: "abc123", status: "sent", delivered: false } });
  const reply = await handleCvDecision(undelivered, { action: "approve", orderId: "abc123" });
  assert.match(reply.text, /could not be sent/i);

  const failing = cvApi({ approve: { ok: false, error: "No order abc123." } });
  const failed = await handleCvDecision(failing, { action: "approve", orderId: "abc123" });
  assert.equal(failed.text, "No order abc123.");
});

test("handleCvDecision survives invalid CLI output", async () => {
  const api = cvApi({ approve: "not json at all" });
  api.logger.warn = () => {};
  const reply = await handleCvDecision(api, { action: "approve", orderId: "abc123" });
  assert.equal(reply.handled, true);
  assert.match(reply.text, /couldn't be updated/i);
});
