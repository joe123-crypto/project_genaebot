import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { handleCvMakerRequest, isCvMakerRequest } from "./cv-maker.js";

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
