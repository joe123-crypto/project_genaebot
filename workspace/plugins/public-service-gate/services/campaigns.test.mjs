import assert from "node:assert/strict";
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
    set(value) { fs.writeFileSync(resultFile, JSON.stringify(value)); } };
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
