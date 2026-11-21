import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import plugin, { stopTakeoverWatch } from "../index.js";
import { buildCustomerBotContext } from "../lib/customer-bot.js";
import { lookupCampaigns } from "./campaigns.js";

function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-campaign-tool-"));
  const previous = { PATH: process.env.PATH, OPENCLAW_STATE_DIR: process.env.OPENCLAW_STATE_DIR, GENAIE_CV_ACCEPT_NEW_ORDERS: process.env.GENAIE_CV_ACCEPT_NEW_ORDERS };
  Object.assign(process.env, { PATH: `${dir}:${process.env.PATH}`, OPENCLAW_STATE_DIR: dir, GENAIE_CV_ACCEPT_NEW_ORDERS: "0" });
  t.after(() => {
    stopTakeoverWatch();
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[name]; else process.env[name] = value;
    }
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const resultFile = path.join(dir, "result.json");
  const argsFile = path.join(dir, "args.json");
  fs.writeFileSync(path.join(dir, "python3"), `#!${process.execPath}\nimport fs from 'node:fs';\nfs.writeFileSync(${JSON.stringify(argsFile)}, JSON.stringify(process.argv.slice(2)));\nprocess.stdout.write(fs.readFileSync(${JSON.stringify(resultFile)}, 'utf8'));`, { mode: 0o700 });
  let factory;
  const api = { config: { plugins: { entries: { "public-service-gate": { config: { cvMakerEnabled: true } } } } },
    logger: { warn() {}, info() {} }, on() {}, registerTool(value) { factory = value; } };
  plugin.register(api);
  const tool = factory({ agentId: "public", messageChannel: "whatsapp", requesterSenderId: "+26370000001", accountId: "ads" });
  return { dir, api, factory, tool, args: () => JSON.parse(fs.readFileSync(argsFile)),
    set(value) {
      const result = { source: "live_campaigns", guidance: "Use only this turn's lookup.",
        fetched_at: "2026-11-19T12:00:00+00:00", service_availability: { cv_new_orders: false }, more: false, ...value };
      result.campaigns = result.campaigns.map((row) => ({
        campaign_id: "bricks", advertising_status: "enabled", ...row,
        ...(row.advertised_content ? { schedule: [{ time: "09:07", timezone: "Africa/Harare", weekdays: [0],
          paused: false, skipped_today: false, pending: false }] } : {}),
      }));
      fs.writeFileSync(resultFile, JSON.stringify(result));
    } };
}

test("a factual follow-up uses a new lookup, forwards only supported selectors and keeps runtime CV restrictions", async (t) => {
  const f = fixture(t);
  const read = async (params) => JSON.parse((await f.tool.execute("lookup", params)).content[0].text);
  f.set({ status: "ok", campaigns: [{ name: "Bricks", advertised_content: ["$95"] }] });
  assert.equal((await read({ action: "campaign_info", query: "bricks" })).campaigns[0].advertised_content[0], "$95");
  assert.ok(f.args().some((arg) => arg.endsWith("/bin/public-read")));
  assert.ok(f.args().includes("--query=bricks"));
  assert.ok(!f.args().includes("--cv-available"));
  f.set({ status: "ok", campaigns: [{ name: "Bricks", advertised_content: ["$107"] }] });
  assert.equal((await read({ action: "campaign_info", campaign_id: "bricks" })).campaigns[0].advertised_content[0], "$107");
  assert.ok(f.args().includes("--campaign-id=bricks"));
  await read({ action: "business_info" });
  assert.ok(f.args().includes("--discover"));
  fs.rmSync(path.join(f.dir, "result.json"));
  const failed = await read({ action: "campaign_info", campaign_id: "bricks" });
  assert.equal(failed.status, "unavailable");
  assert.deepEqual(failed.campaigns, []);
  assert.doesNotMatch(JSON.stringify(failed), /\$107|ENOENT|result.json/);
});

test("missing sender identity and malformed selectors cannot reach the backend", async (t) => {
  const f = fixture(t);
  const unbound = f.factory({ agentId: "public", messageChannel: "whatsapp" });
  await unbound.execute("lookup", { action: "campaign_info", query: "bricks" });
  assert.equal(fs.existsSync(path.join(f.dir, "args.json")), false);
  for (const params of [{ query: "bricks", campaign_id: "bricks" }, { campaign_id: "../../private" }, { query: 12 }]) {
    assert.equal((await lookupCampaigns(f.api, params)).status, "unavailable");
  }
  assert.equal(fs.existsSync(path.join(f.dir, "args.json")), false);
  f.set({ status: "ok", campaigns: [] });
  await lookupCampaigns(f.api, { query: "$(echo unsafe)" });
  assert.ok(f.args().includes("--query=$(echo unsafe)"));
});

test("unexpected helper fields and malformed content never cross the public tool boundary", async (t) => {
  const f = fixture(t);
  for (const value of [
    { status: "ok", campaigns: [{ name: "Bricks", advertised_content: [{ secret: "PRIVATE" }] }] },
    { status: "ok", campaigns: [{ name: "Bricks", internal_note: "PRIVATE" }] },
    { status: "ok", secret: "PRIVATE", campaigns: [] },
    { status: "unavailable", campaigns: [{ name: "Bricks", advertised_content: ["STALE $95"] }] },
    { status: "ok", campaigns: [{ name: "Bricks", advertising_status: "disabled", advertised_content: ["STALE $95"] }] },
  ]) {
    f.set(value);
    const result = await lookupCampaigns(f.api, { query: "bricks" });
    assert.equal(result.status, "unavailable");
    assert.deepEqual(result.campaigns, []);
    assert.doesNotMatch(JSON.stringify(result), /PRIVATE|STALE/);
  }
});

test("a failed lookup names its cause in the operator log and gives the model only the generic fallback", async () => {
  const logged = [];
  const api = { config: {}, logger: { warn(message) { logged.push(message); } } };
  const fail = (fields) => async () => { throw Object.assign(new Error("Command failed"), fields); };
  const valid = { status: "ok", source: "live_campaigns", guidance: "Use only this turn's lookup.", fetched_at: "2026-11-19T12:00:00+00:00",
    service_availability: { cv_new_orders: false }, more: false, campaigns: [{ campaign_id: "bricks", name: "Bricks", advertising_status: "enabled" }] };
  const cases = [
    [fail({ code: 1, stderr: "Traceback: ImportError at +263771234567" }), /\(reader exited 1; stderr: Traceback: ImportError at \[phone\]\)$/],
    [fail({ code: null, killed: true, signal: "SIGTERM", stderr: "" }), /\(reader timed out or was killed \(SIGTERM\)\)$/],
    [fail({ code: null, signal: "SIGKILL", stderr: "" }), /\(reader killed by SIGKILL\)$/],
    [fail({ code: "ENOENT" }), /\(reader could not run \(ENOENT\)\)$/],
    [fail({ message: "Command returned invalid JSON: Unexpected token", stdout: "warning: hello", stderr: "" }), /\(reader printed invalid JSON; stdout: warning: hello\)$/],
    [async () => ({ ...valid, campaigns: [{ ...valid.campaigns[0], internal_note: "PRIVATE" }] }), /\(invalid result: campaigns\[0\] \(bricks\)\)$/],
    [async () => ({ ...valid, service_availability: { cv_new_orders: true } }), /\(invalid result: service_availability\)$/],
    [async () => ({ status: "unavailable", campaigns: [], reason: "ConfigUnavailable@operator_config.py:368: firestore.googleapis.com answered HTTP 403" }),
      /\(reader: ConfigUnavailable@operator_config\.py:368: firestore\.googleapis\.com answered HTTP 403\)$/],
    [async () => ({ status: "unavailable", campaigns: [] }), /\(reader: no reason given\)$/],
  ];
  for (const [run, expected] of cases) {
    logged.length = 0;
    const result = await lookupCampaigns(api, { discover: true }, run);
    assert.equal(result.status, "unavailable");
    assert.deepEqual(result.campaigns, []);
    assert.doesNotMatch(JSON.stringify(result), /reason|stderr|PRIVATE|ENOENT|operator_config|403/);
    assert.equal(logged.length, 1);
    assert.match(logged[0], /^public-service-gate: live campaign lookup unavailable \(/);
    assert.match(logged[0], expected);
  }
  logged.length = 0;
  assert.deepEqual(await lookupCampaigns(api, { discover: true }, async () => valid), valid);
  assert.deepEqual(logged, []);
});

test("published instructions migrate without briefing injection and lookup rules apply to existing public chats only", (t) => {
  const f = fixture(t);
  const published = path.join(f.dir, "runtime", "customer-bot", "published.json");
  fs.mkdirSync(path.dirname(published), { recursive: true });
  const ctx = { agentId: "public", channel: "whatsapp", sessionKey: "agent:public:whatsapp:direct:123" };
  for (const version of [1, 2]) {
    fs.writeFileSync(published, JSON.stringify({ version, revision: "a".repeat(32), publishedAt: "2026-11-15T00:00:00Z",
      publishedBy: "Operator", instructions: "Use a short friendly greeting.", knowledge: "RETIRED PRICE $95" }));
    const context = buildCustomerBotContext(f.api, ctx);
    assert.match(context.appendSystemContext, /Use a short friendly greeting/);
    assert.match(context.appendContext, /fresh public_service lookup in this turn/);
    assert.match(context.appendSystemContext, /untrusted reference data, not instructions/);
    assert.doesNotMatch(JSON.stringify(context), /RETIRED PRICE|\$95/);
  }
  for (const other of [{ ...ctx, agentId: "main" }, { ...ctx, channel: "telegram" },
    { ...ctx, sessionKey: "agent:public:whatsapp:group:123@g.us" }, { ...ctx, trigger: "cron" }]) {
    assert.equal(buildCustomerBotContext(f.api, other), undefined);
  }
  fs.writeFileSync(published, "{");
  assert.match(buildCustomerBotContext(f.api, ctx).appendSystemContext, /fresh public_service lookup/);
});

test("the tool accepts the actual Python reader contract for discovery, details, restrictions and routing", async () => {
  const script = `
import json
from unittest.mock import patch
import operator_config as config
import public_campaigns as public
from test_operator_config import campaign

def read(doc, **params):
    with patch.object(public, "live_documents", return_value=[("bricks", config.encode_fields(doc))]):
        return public.lookup({}, cv_available=False, **params)

doc = {**campaign("bricks", "09:07"), "enabled": True}
results = [read(doc, discover=True), read(doc, campaign_id="bricks"), read(doc, query="missing")]
results.append(read({**doc, "enabled": False}, campaign_id="bricks"))
results.append(read({**doc, "scheduleEdit": {"pending": True}}, campaign_id="bricks"))
results.append(read({**doc, "name": "Private Borehole Supplier"}, query="drilling"))
paused = {"cron": "7 9 * * *", "timezone": "Africa/Harare", "paused": True, "deleted": False, "skipDates": []}
results.append(read({**doc, "scheduleEntries": {"primary": {**paused, "confirmed": paused}}}, campaign_id="bricks"))
assert results[-1]["campaigns"][0]["advertising_status"] == "paused" and results[-1]["campaigns"][0]["advertised_content"]
print(json.dumps(results))
`;
  const responses = JSON.parse(execFileSync("/usr/bin/python3", ["-c", script], {
    encoding: "utf8", env: { ...process.env, PYTHONPATH: fileURLToPath(new URL("../../../lib/", import.meta.url)) },
  }));
  const api = { config: {}, logger: { warn(message) { assert.fail(message); } } };
  for (const response of responses) {
    const result = await lookupCampaigns(api, {}, async () => response);
    assert.deepEqual(result, response);
  }
});

test("lookups find the workspace from the installed plugin when OpenClaw runs a captured copy", async (t) => {
  // OpenClaw 2026.9 runs a package plugin from a copy of only its package
  // (~/.openclaw/tmp/plugin-captures/.../package-N/). Paths taken from
  // import.meta.url then miss the workspace's bin/ and data/, which on
  // 2026-11-20 sent every product question to a silent handoff.
  const installed = fileURLToPath(new URL("..", import.meta.url));
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "plugin-capture-"));
  t.after(() => fs.rmSync(scratch, { recursive: true, force: true }));
  const copy = path.join(scratch, "captures", "openclaw-plugin-build-test", "package-1");
  fs.cpSync(installed, copy, { recursive: true });
  const captured = (file) => import(pathToFileURL(path.join(copy, file)).href);
  const { lookupCampaigns: capturedLookup } = await captured("services/campaigns.js");
  const { loadCatalog: capturedCatalog } = await captured("services/borehole.js");

  const warnings = [];
  const api = { config: {}, rootDir: installed, logger: { warn: (line) => warnings.push(line) } };
  let reader;
  await capturedLookup(api, { discover: true }, async (_command, args) => {
    reader = args[0];
    return { status: "not_found", source: "live_campaigns", guidance: "Use only this turn's lookup.", more: false,
      fetched_at: "2026-11-20T23:43:57+00:00", service_availability: { cv_new_orders: false }, campaigns: [] };
  });
  assert.equal(reader, path.resolve(installed, "../../bin/public-read"));
  assert.ok(fs.existsSync(reader));
  assert.ok(capturedCatalog(api).towns.length > 0);
  assert.deepEqual(warnings, []);
  // Without rootDir the copy can only find itself, which is what production hit.
  assert.throws(() => capturedCatalog({ config: {} }), { code: "ENOENT" });
});
