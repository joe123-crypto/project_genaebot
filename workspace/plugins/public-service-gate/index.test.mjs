import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import plugin, { createLimiter, isQueueFullError, runJsonCommand } from "./index.js";
import { busyReply } from "./lib/runtime.js";
import { REPLY_PACING_ENV, resetReplyPacer } from "./lib/reply-pacing.js";
import { notifyAdminOfIntake, notifyAdminOfLead } from "./services/membership.js";

const MEMBER = "+233208864185";
const UNKNOWN = "+19995550123";
const ADMIN = "+213563719936";

// Reply pacing holds real replies for seconds. The suite runs with it off; the
// pacing tests below switch it on with a fake clock (see withReplyPacing).
process.env[REPLY_PACING_ENV] = "0";

const HANDOFF = /your request is in\. Someone will contact you soon/i;
const PARTNER = "+263775489417";

function registerHandler(config = {}, complete, relevanceConfig = {}) {
  const handlers = [];
  const serviceStatePath =
    config.serviceStatePath ?? path.join(fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-state-")), "state.json");
  let effectiveConfig = config;
  if (!effectiveConfig.memberStoreScript) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-member-"));
    const memberStoreScript = path.join(dir, "whatsapp-invite");
    writeExecutable(
      memberStoreScript,
      `#!/usr/bin/env bash
set -eu
case "$1" in
  status)
    if [ "$3" = "${MEMBER}" ]; then
      printf '{"phone":"${MEMBER}","status":"unlocked","unlocked":true}\\n'
    else
      printf '{"phone":"%s","status":"unknown","unlocked":false}\\n' "$3"
    fi
    ;;
  request) printf '{"ok":true,"phone":"%s","status":"pending","changed":true,"isNew":true}\\n' "$3" ;;
  grandfather) printf '{"ok":true,"phone":"%s","status":"unlocked","changed":true}\\n' "$3" ;;
  *) exit 2 ;;
esac
`,
    );
    effectiveConfig = { ...effectiveConfig, memberStoreScript };
  }
  let toolFactory;
  const api = {
    config: {
      plugins: {
        entries: {
          "public-service-gate": {
            config: {
              enabled: true,
              publicUsers: [MEMBER],
              memberGateEnabled: true,
              serviceStatePath,
              ...effectiveConfig,
            },
          },
          "whatsapp-relevance-gate": {
            config: {
              enabled: true,
              serviceKeywords: [
                "cv",
                "borehole",
                "fruit trees",
              ],
              serviceExamples: [
                "make me a cv",
                "how much is borehole drilling",
                "do you sell mango trees",
              ],
              serviceSummary: "CV making, borehole drilling, and grafted fruit trees",
              useLlmFallback: true,
              ...relevanceConfig,
            },
          },
        },
      },
    },
    runtime: complete
      ? {
          llm: { complete },
        }
      : {},
    logger: {
      debug() {},
      info() {},
      warn() {},
    },
    on(name, callback, options = {}) {
      handlers.push({ name, callback, priority: options?.priority ?? 0 });
    },
    registerTool(factory, options) {
      if (options?.name === "public_service") toolFactory = factory;
    },
  };
  plugin.register(api);
  handlers.sort((a, b) => b.priority - a.priority);
  async function dispatch(event, ctx) {
    for (const { name, callback } of handlers) {
      if (name !== "before_dispatch") continue;
      const reply = await callback(event, ctx);
      if (reply !== undefined) return reply;
    }
    return undefined;
  }
  // Like the real runner: every handler runs in priority order and results
  // merge, until one cancels.
  dispatch.messageSending = async function messageSending(event, ctx) {
    let merged;
    for (const { name, callback } of handlers) {
      if (name !== "message_sending") continue;
      const reply = await callback(event, ctx);
      if (reply === undefined) continue;
      merged = { ...merged, ...reply };
      if (merged.cancel === true) break;
    }
    return merged;
  };
  dispatch.publicTool = (ctx) => toolFactory?.(ctx);
  return dispatch;
}

test("public service tool is exposed only to the public WhatsApp agent with a trusted sender", () => {
  const handler = registerHandler({});
  assert.equal(handler.publicTool({ agentId: "main", requesterSenderId: MEMBER, messageChannel: "whatsapp" }), null);
  assert.equal(handler.publicTool({ agentId: "public", requesterSenderId: "", messageChannel: "whatsapp" }).name, "public_service");
  assert.equal(handler.publicTool({ agentId: "public", requesterSenderId: MEMBER, messageChannel: "telegram" }).name, "public_service");
  const tool = handler.publicTool({ agentId: "public", requesterSenderId: MEMBER, messageChannel: "whatsapp" });
  assert.equal(tool.name, "public_service");
  assert.equal(Object.hasOwn(tool.parameters.properties, "phone"), false);
  assert.deepEqual(tool.parameters.required, ["action"]);
});

test("a disabled public service is omitted from the public service tool schema", async () => {
  const handler = registerHandler({ cvMakerEnabled: false });
  const tool = handler.publicTool({ agentId: "public", requesterSenderId: MEMBER, messageChannel: "whatsapp" });

  assert.deepEqual(tool.parameters.properties.action.enum, ["business_info", "borehole_info", "handoff"]);

  const result = await tool.execute("test", { action: "cv_maker_start" });
  assert.match(result.content[0].text, /isn't available here right now/i);
  assert.doesNotMatch(result.content[0].text, /making a CV/);
});

test("classic mode: enabling the CV maker exposes cv_maker_start and routes to the intro", async () => {
  const handler = registerHandler({ cvMakerEnabled: true, memberGateEnabled: false });
  const tool = handler.publicTool({ agentId: "public", requesterSenderId: MEMBER, messageChannel: "whatsapp" });
  assert.ok(tool.parameters.properties.action.enum.includes("cv_maker_start"));

  const result = await tool.execute("test", { action: "cv_maker_start", text: "make me a cv" });
  assert.match(result.content[0].text, /professional one-page CV/i);
  assert.match(result.content[0].text, /revamp/i);
});

test("cv_maker_start is omitted while the CV maker is disabled", async () => {
  const handler = registerHandler({});
  const tool = handler.publicTool({ agentId: "public", requesterSenderId: MEMBER, messageChannel: "whatsapp" });
  assert.equal(tool.parameters.properties.action.enum.includes("cv_maker_start"), false);
});

test("classic mode: a member CV request defers to the agent when the CV maker is enabled", async () => {
  const handler = registerHandler({ memberGateEnabled: false, cvMakerEnabled: true });
  const reply = await handler(eventFor("can you make me a professional cv", MEMBER), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });
  assert.equal(reply, undefined);
});

test("classic mode: a CV request is unavailable while the CV maker is disabled", async () => {
  const handler = registerHandler({ memberGateEnabled: false });
  const reply = await handler(eventFor("please revamp my cv", MEMBER), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });
  assert.equal(reply.handled, true);
  assert.match(reply.text, /isn't available here right now/i);
});

function eventFor(text, sender = MEMBER) {
  return {
    channel: "whatsapp",
    senderId: sender,
    content: text,
  };
}

function writeExecutable(filePath, body) {
  fs.writeFileSync(filePath, body, { mode: 0o755 });
}

async function withAdminMode(value, fn) {
  const hadValue = Object.prototype.hasOwnProperty.call(process.env, "WHATSAPP_ADMIN_ENABLED");
  const previous = process.env.WHATSAPP_ADMIN_ENABLED;
  if (value === undefined) {
    delete process.env.WHATSAPP_ADMIN_ENABLED;
  } else {
    process.env.WHATSAPP_ADMIN_ENABLED = value;
  }
  try {
    return await fn();
  } finally {
    if (hadValue) {
      process.env.WHATSAPP_ADMIN_ENABLED = previous;
    } else {
      delete process.env.WHATSAPP_ADMIN_ENABLED;
    }
  }
}

function writeServiceState(filePath, phone, service, step, options = {}) {
  fs.writeFileSync(
    filePath,
    JSON.stringify(
      {
        version: 1,
        states: {
          [phone]: {
            phone,
            service,
            step,
            updatedAt: new Date().toISOString(),
            expiresAt: options.expiresAt ?? new Date(Date.now() + 60_000).toISOString(),
            data: options.data ?? {},
          },
        },
      },
      null,
      2,
    ),
  );
}

test("failsafe stop command silently clears active service state before dispatch", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const serviceStatePath = path.join(dir, "state.json");
  writeServiceState(serviceStatePath, MEMBER, "cv-maker", "cv.interview");

  const handler = registerHandler({ serviceStatePath });
  const reply = await handler(eventFor("stop!"), { channelId: "whatsapp", senderId: MEMBER });
  const saved = JSON.parse(fs.readFileSync(serviceStatePath, "utf8"));

  assert.equal(reply.handled, true);
  assert.equal(reply.text, undefined);
  assert.deepEqual(saved.states, {});
});

test("failsafe stop command accepts transcript variants before dispatch", async () => {
  let calls = 0;
  const handler = registerHandler({}, async () => {
    calls += 1;
    return "RELEVANT";
  });

  for (const text of ["stop", "Stop", "stop!"]) {
    const reply = await handler(eventFor(text), { channelId: "whatsapp", senderId: MEMBER });

    assert.equal(reply.handled, true);
    assert.equal(reply.text, undefined);
  }
  assert.equal(calls, 0);
});

test("failsafe stop command handles nested live-style event fields", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const serviceStatePath = path.join(dir, "state.json");
  writeServiceState(serviceStatePath, MEMBER, "cv-maker", "cv.interview");

  let calls = 0;
  const handler = registerHandler({ serviceStatePath }, async () => {
    calls += 1;
    return "RELEVANT";
  });
  const reply = await handler(
    {
      channel: "whatsapp:default",
      sender: { e164: MEMBER },
      message: { text: "Stop" },
    },
    { channelId: "whatsapp:default" },
  );
  const saved = JSON.parse(fs.readFileSync(serviceStatePath, "utf8"));

  assert.equal(reply.handled, true);
  assert.equal(reply.text, undefined);
  assert.equal(calls, 0);
  assert.deepEqual(saved.states, {});
});

test("failsafe stop command suppresses late WhatsApp outbound until next inbound", async () => {
  const handler = registerHandler({});

  const stopReply = await handler(eventFor("stop"), { channelId: "whatsapp", senderId: MEMBER });
  const suppressed = await handler.messageSending(
    { to: MEMBER, content: "Here is the price list you asked for." },
    { channelId: "whatsapp", senderId: MEMBER },
  );

  assert.equal(stopReply.handled, true);
  assert.equal(stopReply.text, undefined);
  assert.equal(suppressed.cancel, true);
  assert.equal(suppressed.cancelReason, "failsafe_stop");

  await handler(eventFor("Tell me a joke about school"), { channelId: "whatsapp", senderId: MEMBER });
  const allowed = await handler.messageSending(
    { to: MEMBER, content: "Fresh response after the next inbound message." },
    { channelId: "whatsapp", senderId: MEMBER },
  );

  assert.equal(allowed, undefined);
});

test("outbound WhatsApp replies never leave with an em dash", async () => {
  const handler = registerHandler({});

  const rewritten = await handler.messageSending(
    { to: MEMBER, content: "All our services operate all around Zimbabwe \u2014 where are you from?" },
    { channelId: "whatsapp", senderId: MEMBER },
  );
  assert.equal(rewritten.content, "All our services operate all around Zimbabwe, where are you from?");
  assert.equal(rewritten.cancel, undefined);

  const clean = await handler.messageSending(
    { to: MEMBER, content: "Pa 60m: $1,450." },
    { channelId: "whatsapp", senderId: MEMBER },
  );
  assert.equal(clean, undefined);

  const otherChannel = await handler.messageSending(
    { to: MEMBER, content: "Water survey \u2014 $100." },
    { channelId: "telegram", senderId: MEMBER },
  );
  assert.equal(otherChannel, undefined);
});

test("a failsafe stop still cancels a reply that also needs an em dash rewrite", async () => {
  const handler = registerHandler({});

  await handler(eventFor("stop"), { channelId: "whatsapp", senderId: MEMBER });
  const suppressed = await handler.messageSending(
    { to: MEMBER, content: "Water survey \u2014 $100." },
    { channelId: "whatsapp", senderId: MEMBER },
  );

  assert.equal(suppressed.cancel, true);
  assert.equal(suppressed.cancelReason, "failsafe_stop");
  assert.equal(suppressed.content, undefined);
});

test("failsafe stop command claims unknown sender before member or LLM checks", async () => {
  let calls = 0;
  const handler = registerHandler({}, async () => {
    calls += 1;
    return "RELEVANT";
  });
  const reply = await handler(eventFor("stop!", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.equal(reply.text, undefined);
  assert.equal(calls, 0);
});

test("failsafe stop command claims admin before admin bypass", async () => {
  await withAdminMode("true", async () => {
    const handler = registerHandler({ whatsappAdminUsers: [ADMIN] });
    const reply = await handler(eventFor("STOP", ADMIN), {
      channelId: "whatsapp",
      senderId: ADMIN,
    });

    assert.equal(reply.handled, true);
    assert.equal(reply.text, undefined);
  });
});

test("classic mode: failsafe stop command requires a one-word stop message", async () => {
  const handler = registerHandler({ memberGateEnabled: false });

  assert.equal(await handler(eventFor("please stop"), { channelId: "whatsapp", senderId: MEMBER }), undefined);
  assert.equal(await handler(eventFor("don't stop"), { channelId: "whatsapp", senderId: MEMBER }), undefined);
  assert.equal(await handler(eventFor("unstoppable"), { channelId: "whatsapp", senderId: MEMBER }), undefined);
});

// Member-store stub that logs every call. `isNew` controls whether the lead is
// a first contact (which alerts the admin once).
function leadStore(argsLog, { isNew = true } = {}) {
  return `#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "${argsLog}"
case "$1" in
  status) printf '{"phone":"%s","status":"pending","unlocked":false}\\n' "$3" ;;
  request) printf '{"ok":true,"phone":"%s","status":"pending","changed":true,"isNew":${isNew}}\\n' "$3" ;;
  *) exit 2 ;;
esac
`;
}

// Existing member store stub: never a new lead, so no admin alert.
function intakeStore(argsLog) {
  return leadStore(argsLog, { isNew: false });
}

// Puts a fake `openclaw` CLI on PATH that logs every send, for the duration of fn.
async function withFakeOpenclaw(dir, fn) {
  const log = path.join(dir, "openclaw.log");
  writeExecutable(path.join(dir, "openclaw"), `#!/usr/bin/env bash\nprintf '%s\\n' "$*" >> ${JSON.stringify(log)}\n`);
  const previousPath = process.env.PATH;
  process.env.PATH = `${dir}${path.delimiter}${previousPath}`;
  try {
    return await fn(() => (fs.existsSync(log) ? fs.readFileSync(log, "utf8") : ""));
  } finally {
    process.env.PATH = previousPath;
  }
}

function intakeHandler(extraConfig = {}, store = leadStore) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-intake-"));
  const memberStoreScript = path.join(dir, "whatsapp-invite");
  const argsLog = path.join(dir, "member-args.txt");
  const serviceStatePath = path.join(dir, "state.json");
  writeExecutable(memberStoreScript, store(argsLog));
  const handler = registerHandler({ memberStoreScript, serviceStatePath, ...extraConfig });
  handler.dir = dir;
  handler.memberArgs = () => (fs.existsSync(argsLog) ? fs.readFileSync(argsLog, "utf8") : "");
  handler.handoffsPath = path.join(dir, "handoffs.json");
  return handler;
}

function publicToolFor(handler, sender) {
  return handler.publicTool({ agentId: "public", requesterSenderId: sender, messageChannel: "whatsapp" });
}

test("a new sender's message reaches the agent, is recorded as a lead, and alerts the admin once", async () => {
  await withAdminMode("true", async () => {
    const handler = intakeHandler({ whatsappAdminUsers: [ADMIN] });
    await withFakeOpenclaw(handler.dir, async (sent) => {
      const reply = await handler(eventFor("Hello 5000 to Bindura hw much", UNKNOWN), {
        channelId: "whatsapp",
        senderId: UNKNOWN,
      });

      // Not claimed: the public agent answers. No menu, no silent hold.
      assert.equal(reply, undefined);
      assert.match(handler.memberArgs(), /request --phone \+19995550123 --message Hello 5000 to Bindura hw much/);
      assert.match(sent(), new RegExp(`--target \\${ADMIN}`));
      assert.match(sent(), /New chat from \+19995550123/);
      assert.match(sent(), /--account default --target/);
      assert.doesNotMatch(sent(), /allow/);
    });
  });
});

test("lead and intake alerts use default even when the configured default sender is ads", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "admin-alert-account-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const api = {
    config: {
      channels: { whatsapp: { defaultAccount: "ads" } },
      plugins: { entries: { "public-service-gate": {
        config: { whatsappAdminUsers: [ADMIN] },
      } } },
    },
    logger: { warn(message) { assert.fail(message); } },
  };
  await withFakeOpenclaw(dir, async (sent) => {
    await notifyAdminOfLead(api, UNKNOWN, "Bricks enquiry");
    await notifyAdminOfIntake(api, UNKNOWN, "Building materials", "5000 bricks");
    const sends = sent().split("\n").filter(line => line.startsWith("message send"));
    assert.equal(sends.length, 2);
    for (const send of sends) {
      assert.match(send, /--channel whatsapp --account default --target/);
      assert.doesNotMatch(send, /--account ads/);
    }
  });
});

test("an unmapped LID reaches public tools and cannot impersonate an admin phone", async () => {
  const lid = `${ADMIN.slice(1)}@lid`;
  const handler = intakeHandler({ whatsappAdminUsers: [ADMIN] });
  await withFakeOpenclaw(handler.dir, async () => {
    assert.equal(await handler(eventFor("hello", lid), { channelId: "whatsapp", senderId: lid, accountId: "ads" }), undefined);
    assert.ok(handler.memberArgs().includes(lid));
    const infoPath = path.join(handler.dir, "business-info.md");
    fs.writeFileSync(infoPath, "Current prices for opaque contacts");
    const tool = publicToolFor(registerHandler({ businessInfoPath: infoPath }), lid);
    const result = await tool.execute("test", { action: "business_info" });
    assert.match(result.content[0].text, /Current prices for opaque contacts/);
    const handoff = await publicToolFor(handler, lid).execute("test", { action: "handoff", category: "building_materials", details: "5000 bricks" });
    assert.match(handoff.content[0].text, HANDOFF);
    assert.deepEqual(await handler(eventFor("hello again", lid), { channelId: "whatsapp", senderId: lid }), { handled: true });
  });
});

test("a returning sender reaches the agent without another admin alert", async () => {
  await withAdminMode("true", async () => {
    const handler = intakeHandler({ whatsappAdminUsers: [ADMIN] }, intakeStore);
    await withFakeOpenclaw(handler.dir, async (sent) => {
      const reply = await handler(eventFor("hi", UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });
      assert.equal(reply, undefined);
      assert.equal(sent(), "");
    });
  });
});

test("greetings, digits and off-topic text all reach the agent", async () => {
  const handler = intakeHandler();
  for (const text of ["hi", "2", "menu", "tell me the bitcoin price now", "JOIN-ABC123", "invite link"]) {
    const reply = await handler(eventFor(text, UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });
    assert.equal(reply, undefined, `expected "${text}" to reach the agent`);
  }
});

test("business_info returns the business info page", async () => {
  const handler = intakeHandler();
  const infoPath = path.join(handler.dir, "business-info.md");
  fs.writeFileSync(infoPath, "# Info\nRed commons: $95 per 1000\n");
  const tool = publicToolFor(registerHandler({ businessInfoPath: infoPath }), UNKNOWN);

  const result = await tool.execute("test", { action: "business_info" });
  assert.match(result.content[0].text, /Red commons: \$95 per 1000/);
});

test("business_info fails soft and points at a handoff when the page is missing", async () => {
  const tool = publicToolFor(registerHandler({ businessInfoPath: "/nonexistent/business-info.md" }), UNKNOWN);
  const result = await tool.execute("test", { action: "business_info" });
  assert.match(result.content[0].text, /unavailable/i);
  assert.match(result.content[0].text, /handoff/i);
});

test("borehole_info uses the bound sender and hands internal provider context only to the operator", async () => {
  const handler = intakeHandler({ whatsappAdminUsers: [ADMIN] });
  await withFakeOpenclaw(handler.dir, async (sent) => {
    const tool = publicToolFor(handler, UNKNOWN);
    const result = await tool.execute("test", { action: "borehole_info", location: "Harare", service: "drilling", depth_m: 60 });
    assert.equal(JSON.parse(result.content[0].text).offers[0].drilling_subtotal, 1170);
    assert.doesNotMatch(result.content[0].text, /Big Player|Harare|providerId/);
    const handoff = await tool.execute("test", { action: "handoff", category: "borehole", details: "Site visit needed" });
    assert.match(handoff.content[0].text, HANDOFF);
    assert.doesNotMatch(handoff.content[0].text, /Big Player|Harare/);
    assert.match(sent(), /Big Player, Harare/);
    assert.match(sent(), new RegExp(ADMIN.replace(/\+/g, "\\+")));
    assert.doesNotMatch(sent(), new RegExp(PARTNER.replace(/\+/g, "\\+")));
  });
});

test("the deployed business info page carries the core facts", () => {
  const page = fs.readFileSync(new URL("../../docs/business-info.md", import.meta.url), "utf8");
  assert.match(page, /All our services operate all around Zimbabwe\. Where are you from\?/);
  assert.match(page, /Do not describe\s+us as based in Bindura/);
  assert.match(page, /Transport within local Bindura: \$550/);
  assert.match(page, /action `borehole_info`/);
  assert.doesNotMatch(page, /Drilling, first 40m: \$950/);
  assert.match(page, /Big: \$20 per tree/);
  assert.match(page, /\$3 per tree, minimum 20 trees/);
  assert.match(page, /medium or big: available from 100 trees/);
});

test("handoff of a partner product forwards to the partner, replies, then silences the customer", async () => {
  for (const category of ["building_materials", "fruit_trees"]) {
    const handler = intakeHandler();
    await withFakeOpenclaw(handler.dir, async (sent) => {
      const tool = publicToolFor(handler, UNKNOWN);
      const result = await tool.execute("test", {
        action: "handoff",
        category,
        details: "5000 red commons, deliver to Chipadze",
      });

      assert.match(result.content[0].text, HANDOFF);
      assert.match(sent(), new RegExp(`--target \\${PARTNER}`));
      assert.match(sent(), /Customer: \+19995550123/);
      assert.match(sent(), /5000 red commons, deliver to Chipadze/);

      const later = await handler(eventFor("hello?", UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });
      assert.equal(later.handled, true, `${category}: later messages are claimed`);
      assert.equal(later.text, undefined, `${category}: and get no reply`);
    });
  }
});

test("handoff of other categories forwards nothing but still silences the customer", async () => {
  for (const category of ["borehole", "cv", "marketing", "other", "not-a-category"]) {
    const handler = intakeHandler();
    await withFakeOpenclaw(handler.dir, async (sent) => {
      const result = await publicToolFor(handler, UNKNOWN).execute("test", {
        action: "handoff",
        category,
        details: "drilling and installation quote",
      });
      assert.match(result.content[0].text, HANDOFF);
      assert.equal(sent(), "", `${category}: nothing forwarded`);
      const later = await handler(eventFor("any update?", UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });
      assert.equal(later.handled, true);
      assert.equal(later.text, undefined);
    });
  }
});

test("admin resume lets the bot reply to a handed-off customer again", async () => {
  await withAdminMode("true", async () => {
    const handler = intakeHandler({ whatsappAdminUsers: [ADMIN] });
    await withFakeOpenclaw(handler.dir, async () => {
      await publicToolFor(handler, UNKNOWN).execute("test", { action: "handoff", category: "borehole", details: "x" });

      const resumed = await handler(eventFor(`resume ${UNKNOWN}`, ADMIN), { channelId: "whatsapp", senderId: ADMIN });
      assert.equal(resumed.handled, true);
      assert.match(resumed.text, /Resumed \+19995550123/);

      const again = await handler(eventFor("hi", UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });
      assert.equal(again, undefined);

      const noop = await handler(eventFor(`resume ${UNKNOWN}`, ADMIN), { channelId: "whatsapp", senderId: ADMIN });
      assert.match(noop.text, /was not handed off/);
    });
  });
});

test("classic mode: a handed-off member is held silently too", async () => {
  const handler = intakeHandler({ memberGateEnabled: false });
  await withFakeOpenclaw(handler.dir, async () => {
    await publicToolFor(handler, MEMBER).execute("test", { action: "handoff", category: "borehole", details: "x" });
    const reply = await handler(eventFor("hello?", MEMBER), { channelId: "whatsapp", senderId: MEMBER });
    assert.equal(reply.handled, true);
    assert.equal(reply.text, undefined);
  });
});

test("mid-interview words like help or menu stay in the CV flow; only cancel leaves it", async () => {
  const handler = cvHandler();
  const ctx = { channelId: "whatsapp", senderId: UNKNOWN };
  await handler.startCv();
  for (const text of ["help", "menu", "services"]) {
    const turn = await handler(eventFor(text, UNKNOWN), ctx);
    assert.equal(turn.text, "What is your full name?", `"${text}" is relayed to the interview`);
  }
  assert.equal(await handler(eventFor("cancel", UNKNOWN), ctx), undefined);
});

test("a non-admin resume command is ordinary text for the agent", async () => {
  const handler = intakeHandler({ whatsappAdminUsers: [ADMIN] });
  const reply = await handler(eventFor(`resume ${MEMBER}`, UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });
  assert.equal(reply, undefined);
});

test("classic mode: member off-scope text falls through to the agent without relevance calls", async () => {
  let calls = 0;
  const handler = registerHandler(
    { memberGateEnabled: false },
    async () => {
      calls += 1;
      return "RELEVANT";
    },
    { rejectionMessage: "Please choose a supported service." },
  );

  const reply = await handler(eventFor("Tell me a joke about school"), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });

  assert.equal(reply, undefined);
  assert.equal(calls, 0);
});

test("classic mode: member text falls through with nested live-style event fields", async () => {
  const handler = registerHandler({ memberGateEnabled: false }, undefined, { rejectionMessage: "Please choose a supported service." });
  const reply = await handler(
    {
      channel: "whatsapp:default",
      sender: { e164: MEMBER },
      message: { text: "Tell me a joke about football" },
    },
    { channelId: "whatsapp:default" },
  );

  assert.equal(reply, undefined);
});

test("classic mode: member service messages do not call deterministic service helpers", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const cvMakerScript = path.join(dir, "cv-maker");
  const helperLog = path.join(dir, "helper.log");

  writeExecutable(
    cvMakerScript,
    `#!/usr/bin/env bash
printf '%s %s\\n' "$0" "$*" >> "${helperLog}"
exit 2
`,
  );
  const handler = registerHandler({ memberGateEnabled: false, cvMakerScript });
  for (const text of [
    "Hello",
    "1",
    "2",
    "3",
    "4",
    "Done",
  ]) {
    const reply = await handler(eventFor(text), { channelId: "whatsapp", senderId: MEMBER });
    assert.equal(reply, undefined, `expected "${text}" to reach the agent`);
  }
  assert.equal(fs.existsSync(helperLog), false);
});

test("classic mode: active service state no longer short-circuits member continuations", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const serviceStatePath = path.join(dir, "state.json");
  writeServiceState(serviceStatePath, MEMBER, "cv-maker", "cv.interview");

  const handler = registerHandler({ memberGateEnabled: false, serviceStatePath });
  for (const text of ["2", "menu", "tell me more", "continue please"]) {
    const reply = await handler(eventFor(text), { channelId: "whatsapp", senderId: MEMBER });
    assert.equal(reply, undefined, `expected "${text}" to reach the agent`);
  }
  const saved = JSON.parse(fs.readFileSync(serviceStatePath, "utf8"));
  assert.equal(saved.states[MEMBER]?.step, "cv.interview");
});

test("WhatsApp message without a sender fails closed before agent dispatch", async () => {
  const handler = registerHandler({});
  const reply = await handler({ channel: "whatsapp", content: "Tell me a joke" }, { channelId: "whatsapp" });

  assert.equal(reply.handled, true);
  assert.equal(reply.text, undefined);
});

test("admin sender still bypasses public-service gate", async () => {
  await withAdminMode("true", async () => {
    const handler = registerHandler({ whatsappAdminUsers: [ADMIN] });
    const reply = await handler(eventFor("hi", ADMIN), { channelId: "whatsapp", senderId: ADMIN });

    assert.equal(reply, undefined);
  });
});

test("admin sender is treated as a normal public user when WhatsApp admin mode is disabled", async () => {
  await withAdminMode("false", async () => {
    const handler = intakeHandler({ whatsappAdminUsers: [ADMIN] }, intakeStore);
    const reply = await handler(eventFor("hi", ADMIN), { channelId: "whatsapp", senderId: ADMIN });

    // With admin mode off, ADMIN is not special: the message goes through the
    // public gate to the public agent like anyone else's.
    assert.equal(reply, undefined);
  });
});

test("admin allow command unlocks the target and is claimed", async () => {
  await withAdminMode("true", async () => {
    for (const command of ["allow", "grandfather"]) {
      const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
      const memberStoreScript = path.join(dir, "whatsapp-invite");
      const argsLog = path.join(dir, "allow-args.txt");
      const serviceStatePath = path.join(dir, "state.json");
      writeExecutable(
        memberStoreScript,
        `#!/usr/bin/env bash
set -eu
case "$1" in
  grandfather)
    printf '%s\\n' "$*" > "${argsLog}"
    printf '{"ok":true,"phone":"$3","status":"unlocked","changed":true}\\n'
    ;;
  *) exit 2 ;;
esac
`,
      );

      const handler = registerHandler({ whatsappAdminUsers: [ADMIN], memberStoreScript, serviceStatePath });
      const reply = await handler(eventFor(`${command} +263788390903`, ADMIN), {
        channelId: "whatsapp",
        senderId: ADMIN,
      });

      assert.equal(reply.handled, true, `expected "${command}" to be claimed`);
      assert.match(reply.text, /Allowed \+263788390903: unlocked/);
      assert.equal(fs.readFileSync(argsLog, "utf-8").trim(), "grandfather --phone +263788390903");
      // No menu is sent and no intake slot is primed any more.
      assert.equal(fs.existsSync(serviceStatePath), false);
    }
  });
});

test("admin allow command is not honored when WhatsApp admin mode is disabled", async () => {
  await withAdminMode("false", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
    const memberStoreScript = path.join(dir, "whatsapp-invite");
    const argsLog = path.join(dir, "allow-args.txt");
    writeExecutable(
      memberStoreScript,
      `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"phone":"%s","status":"unknown","unlocked":false}\\n' "$3" ;;
  grandfather)
    printf '%s\\n' "$*" > "${argsLog}"
    printf '{"ok":true,"phone":"$3","status":"unlocked","changed":true}\\n'
    ;;
  *) exit 2 ;;
esac
`,
    );

    const handler = registerHandler({ whatsappAdminUsers: [ADMIN], memberStoreScript }, async () => "IRRELEVANT");
    const reply = await handler(eventFor("allow +263788390903", ADMIN), {
      channelId: "whatsapp",
      senderId: ADMIN,
    });

    // Not a command: it is ordinary text for the public agent.
    assert.equal(reply, undefined);
    assert.equal(fs.existsSync(argsLog), false);
  });
});

test("non-admin allow command does not unlock and is ordinary text for the agent", async () => {
  const handler = intakeHandler({ whatsappAdminUsers: [ADMIN] });
  const reply = await handler(eventFor("allow +263788390903", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply, undefined);
  assert.doesNotMatch(handler.memberArgs(), /grandfather/);
});

test("runJsonCommand parses JSON stdout", async () => {
  const result = await runJsonCommand("/bin/echo", ['{"status":"ok","count":2}']);

  assert.deepEqual(result, { status: "ok", count: 2 });
});

test("public canned replies use the concise style", () => {
  assert.equal(busyReply().text, "I'm busy right now. Try again shortly.");

  const files = [
    "lib/runtime.js",
    "services/membership.js",
    "services/greeting.js",
    "services/intake.js",
    "services/cv-maker.js",
    "services/handoff.js",
    "lib/handoffs.js",
    "lib/sibling-lines.js",
  ];
  for (const file of [...files, "lib/style.js"]) {
    const source = fs.readFileSync(new URL(file, import.meta.url), "utf8");
    assert.doesNotMatch(source, /[\u2012-\u2015]/, `${file} contains an em or en dash`);
  }

  const docs = [
    "public-whatsapp-system-prompt.txt",
    "public-services.md",
    "business-info.md",
  ];
  for (const file of docs) {
    const source = fs.readFileSync(new URL(`../../docs/${file}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /[\u2012-\u2015]/, `${file} contains an em or en dash`);
  }

  const prompt = fs.readFileSync(
    new URL("../../docs/public-whatsapp-system-prompt.txt", import.meta.url),
    "utf8",
  );
  assert.match(prompt, /one or two short sentences is a whole reply/i);
  assert.match(prompt, /Never use an em dash or an en dash/i);
  assert.match(prompt, /Answer the question and stop/i);
  assert.match(prompt, /Never pitch a service the customer did not ask about/i);
  assert.match(prompt, /call `public_service` with action `business_info`/i);
  assert.match(prompt, /Never guess/);
  assert.match(prompt, /unrelated to the business[^\n]*list what we offer/i);
});

test("runJsonCommand rejects non-JSON stdout with stdout attached", async () => {
  await assert.rejects(
    runJsonCommand("/bin/echo", ["not-json"]),
    (err) => {
      assert.match(err.message, /invalid JSON/);
      assert.equal(err.stdout.trim(), "not-json");
      return true;
    },
  );
});

test("runJsonCommand rejects timed-out commands", async () => {
  await assert.rejects(
    runJsonCommand(process.execPath, ["-e", "setTimeout(() => {}, 1000)"], { timeoutMs: 25 }),
    (err) => {
      assert.equal(err.killed, true);
      return true;
    },
  );
});

test("createLimiter caps concurrency and rejects when the queue is full", async () => {
  const limiter = createLimiter({ name: "test", maxConcurrent: 1, maxQueue: 1 });
  let releaseFirst;
  let active = 0;
  let maxActive = 0;

  const first = limiter(
    () =>
      new Promise((resolve) => {
        active += 1;
        maxActive = Math.max(maxActive, active);
        releaseFirst = () => {
          active -= 1;
          resolve("first");
        };
      }),
  );
  const second = limiter(async () => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    active -= 1;
    return "second";
  });

  await assert.rejects(limiter(async () => "third"), (err) => {
    assert.equal(isQueueFullError(err), true);
    return true;
  });

  releaseFirst();

  assert.equal(await first, "first");
  assert.equal(await second, "second");
  assert.equal(maxActive, 1);
});

// ---------------------------------------------------------------------------
// Reply pacing (lib/reply-pacing.js wired into the hooks)
// ---------------------------------------------------------------------------

// Pacing on, with a clock that only moves when the pacer sleeps.
async function withReplyPacing(fn) {
  const clock = { at: 1_000_000 };
  const sleeps = [];
  resetReplyPacer({
    rng: () => 0.5,
    now: () => clock.at,
    sleep: async (ms) => {
      sleeps.push(ms);
      clock.at += ms;
    },
  });
  process.env[REPLY_PACING_ENV] = "1";
  try {
    return await fn({ clock, sleeps });
  } finally {
    process.env[REPLY_PACING_ENV] = "0";
    resetReplyPacer();
  }
}

test("a customer's message waits before the agent, and the reply waits before it is sent", async () => {
  await withReplyPacing(async ({ clock, sleeps }) => {
    const handler = intakeHandler({}, intakeStore);
    const ctx = { channelId: "whatsapp", senderId: UNKNOWN, accountId: "default" };
    const arrived = clock.at;

    assert.equal(await handler(eventFor("How much are bricks?", UNKNOWN), ctx), undefined);
    assert.equal(sleeps.length, 1, "one pause before the agent starts");
    assert.ok(sleeps[0] >= 5000);

    const sent = await handler.messageSending({ to: UNKNOWN, content: "Red commons are $95 per thousand." }, ctx);
    assert.equal(sent, undefined, "pacing never rewrites or cancels");
    assert.equal(sleeps.length, 2, "one pause while the reply is typed");
    assert.ok(clock.at - arrived >= 8000 && clock.at - arrived <= 40000);
  });
});

test("a reply the gate sends itself is held at send time instead", async () => {
  await withReplyPacing(async ({ sleeps }) => {
    const handler = cvHandler();
    const ctx = { channelId: "whatsapp", senderId: UNKNOWN, accountId: "default" };
    await handler.startCv();

    const turn = await handler(eventFor("Jane", UNKNOWN), ctx);
    assert.equal(turn.text, "What is your full name?");
    assert.deepEqual(sleeps, [], "a claimed message is not delayed before the gate answers");

    await handler.messageSending({ to: UNKNOWN, content: turn.text }, ctx);
    assert.equal(sleeps.length, 1);
    assert.ok(sleeps[0] >= 5000 + 1500, "it owes the read pause and the typing time");
  });
});

test("the operator, broadcasts and other channels are never paced", async () => {
  await withReplyPacing(async ({ sleeps }) => {
    await withAdminMode("true", async () => {
      const handler = intakeHandler({ whatsappAdminUsers: [ADMIN] }, intakeStore);
      const adminCtx = { channelId: "whatsapp", senderId: ADMIN, accountId: "default" };

      assert.equal(await handler(eventFor("status", ADMIN), adminCtx), undefined);
      await handler.messageSending({ to: ADMIN, content: "All campaigns ran." }, adminCtx);
      // A promo post and an alert: nobody wrote first.
      await handler.messageSending(
        { to: "120363407177302513@g.us", content: "Daily advert", metadata: { accountId: "ads" } },
        { channelId: "whatsapp", accountId: "ads", conversationId: "120363407177302513@g.us" },
      );
      await handler.messageSending({ to: MEMBER, content: "Your CV is ready." }, { channelId: "whatsapp" });
      await handler.messageSending({ to: UNKNOWN, content: "Hello" }, { channelId: "telegram", senderId: UNKNOWN });
      assert.deepEqual(sleeps, []);
    });
  });
});

test("a cancelled reply is not held, and a rewritten one still is", async () => {
  await withReplyPacing(async ({ sleeps }) => {
    const handler = intakeHandler({}, intakeStore);
    const ctx = { channelId: "whatsapp", senderId: UNKNOWN, accountId: "default" };

    await handler(eventFor("How much is a survey?", UNKNOWN), ctx);
    const rewritten = await handler.messageSending({ to: UNKNOWN, content: "Water survey \u2014 $100." }, ctx);
    assert.equal(rewritten.content, "Water survey, $100.");
    assert.equal(sleeps.length, 2);

    await handler(eventFor("stop", UNKNOWN), ctx);
    const suppressed = await handler.messageSending({ to: UNKNOWN, content: "One more thing." }, ctx);
    assert.equal(suppressed.cancel, true);
    assert.equal(sleeps.length, 2, "nothing is held once the send is cancelled");
  });
});

test("with the kill switch off, nothing is delayed", async () => {
  await withReplyPacing(async ({ sleeps }) => {
    process.env[REPLY_PACING_ENV] = "0";
    const handler = intakeHandler({}, intakeStore);
    const ctx = { channelId: "whatsapp", senderId: UNKNOWN, accountId: "default" };

    await handler(eventFor("How much are bricks?", UNKNOWN), ctx);
    await handler.messageSending({ to: UNKNOWN, content: "Red commons are $95 per thousand." }, ctx);
    assert.deepEqual(sleeps, []);
  });
});

// ---------------------------------------------------------------------------
// Paid CV order flow (started by the agent via cv_maker_start)
// ---------------------------------------------------------------------------

const CV_ORDER_ID = "Ab3-_x";

function cvMakerCli() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-cv-"));
  const script = path.join(dir, "cv-maker");
  const log = path.join(dir, "cv-calls.log");
  writeExecutable(
    script,
    `#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "${log}"
case "$1" in
  status) printf '{"ok":true,"orderId":"","status":""}\\n' ;;
  interview-turn)
    case "$*" in
      *--message=done*)
        printf '{"ok":true,"orderId":"${CV_ORDER_ID}","status":"awaiting_payment","reply":"All set.","complete":true}\\n' ;;
      *)
        printf '{"ok":true,"orderId":"${CV_ORDER_ID}","status":"interviewing","reply":"What is your full name?","complete":false}\\n' ;;
    esac
    ;;
  paid) printf '{"ok":true,"orderId":"${CV_ORDER_ID}","status":"awaiting_approval"}\\n' ;;
  approve) printf '{"ok":true,"orderId":"${CV_ORDER_ID}","status":"sent","delivered":true,"filename":"cv.pdf"}\\n' ;;
  deny) printf '{"ok":true,"orderId":"${CV_ORDER_ID}","status":"rejected"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );
  return {
    dir,
    script,
    calls: () => (fs.existsSync(log) ? fs.readFileSync(log, "utf8").trim().split("\n").filter(Boolean) : []),
  };
}

function cvHandler(extraConfig = {}) {
  const cli = cvMakerCli();
  const serviceStatePath = path.join(cli.dir, "state.json");
  const memberStoreScript = path.join(cli.dir, "whatsapp-invite");
  writeExecutable(memberStoreScript, intakeStore(path.join(cli.dir, "member-args.txt")));
  const handler = registerHandler({
    cvMakerEnabled: true,
    cvMakerScript: cli.script,
    memberStoreScript,
    serviceStatePath,
    ...extraConfig,
  });
  handler.cvCalls = cli.calls;
  handler.startCv = (sender = UNKNOWN) =>
    publicToolFor(handler, sender).execute("test", { action: "cv_maker_start", text: "yes" });
  handler.serviceStatePath = serviceStatePath;
  return handler;
}

test("cv_maker_start pins the sender to the automated CV flow", async () => {
  const handler = cvHandler();

  const started = await handler.startCv();
  assert.match(started.content[0].text, /US\$5/);
  assert.match(started.content[0].text, /EcoCash/);
  // The order itself is created by the first relayed turn. Entering the flow
  // resolves the sender's real order status before deciding where to resume.
  assert.deepEqual(handler.cvCalls(), [`status --phone=${UNKNOWN}`]);
  const saved = JSON.parse(fs.readFileSync(handler.serviceStatePath, "utf8"));
  assert.equal(saved.states[UNKNOWN]?.service, "cv");
});

test("without cv_maker_start a CV request is just conversation for the agent", async () => {
  const handler = cvHandler();
  const reply = await handler(eventFor("please revamp my cv", UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });
  assert.equal(reply, undefined);
  assert.deepEqual(handler.cvCalls(), []);
});

test("cv_maker_start is refused while the CV maker is disabled", async () => {
  const handler = cvHandler({ cvMakerEnabled: false });
  const result = await handler.startCv();
  assert.match(result.content[0].text, /isn't available here right now/i);
  assert.deepEqual(handler.cvCalls(), []);
});

test("CV flow turns relay to the CLI and the payment message uses the configured values", async () => {
  const handler = cvHandler({
    cvMakerPriceText: "US$9",
    cvMakerPaymentDetails: "TestCash 0000 (Test Payee)",
  });
  const ctx = { channelId: "whatsapp", senderId: UNKNOWN };

  await handler.startCv();

  const turn = await handler(eventFor("Jane", UNKNOWN), ctx);
  assert.equal(turn.text, "What is your full name?");

  const priced = await handler(eventFor("done", UNKNOWN), ctx);
  assert.match(priced.text, /All set\./);
  assert.match(priced.text, /US\$9/);
  assert.match(priced.text, /TestCash 0000 \(Test Payee\)/);

  const paid = await handler(eventFor("paid", UNKNOWN), ctx);
  assert.match(paid.text, /confirming your payment/i);

  assert.deepEqual(handler.cvCalls(), [
    `status --phone=${UNKNOWN}`,
    `interview-turn --phone=${UNKNOWN} --message=Jane`,
    `interview-turn --phone=${UNKNOWN} --message=done`,
    `paid --phone=${UNKNOWN}`,
  ]);
  const saved = JSON.parse(fs.readFileSync(handler.serviceStatePath, "utf8"));
  assert.equal(saved.states[UNKNOWN]?.step, "cv.awaiting_approval");
});

test("cancel escapes the CV flow back to the agent", async () => {
  const handler = cvHandler();
  const ctx = { channelId: "whatsapp", senderId: UNKNOWN };

  await handler.startCv();
  const back = await handler(eventFor("cancel", UNKNOWN), ctx);

  assert.equal(back, undefined);
  assert.deepEqual(handler.cvCalls(), [`status --phone=${UNKNOWN}`]);
  const saved = JSON.parse(fs.readFileSync(handler.serviceStatePath, "utf8"));
  assert.equal(saved.states[UNKNOWN], undefined);
});

test("admin cv approve and cv deny settle the order", async () => {
  await withAdminMode("true", async () => {
    for (const [command, expected] of [["approve", /approved and sent/i], ["deny", /denied/i]]) {
      const handler = cvHandler({ whatsappAdminUsers: [ADMIN] });
      const reply = await handler(eventFor(`cv ${command} ${CV_ORDER_ID}`, ADMIN), {
        channelId: "whatsapp",
        senderId: ADMIN,
      });

      assert.equal(reply.handled, true, `expected "cv ${command}" to be claimed`);
      assert.match(reply.text, expected);
      assert.deepEqual(handler.cvCalls(), [`${command} --order=${CV_ORDER_ID}`]);
    }
  });
});

// This is the whole payment control: anyone could otherwise approve their own
// order for free, so a non-admin must never reach the CLI.
test("a non-admin cv approve is ignored and falls through as ordinary text", async () => {
  await withAdminMode("true", async () => {
    const handler = cvHandler({ whatsappAdminUsers: [ADMIN] });
    const reply = await handler(eventFor(`cv approve ${CV_ORDER_ID}`, UNKNOWN), {
      channelId: "whatsapp",
      senderId: UNKNOWN,
    });

    // Ordinary text: it goes to the public agent like any other message.
    assert.equal(reply, undefined);
    assert.deepEqual(handler.cvCalls(), []);
  });
});

// The id is interpolated into the CLI argv, so an id-shaped run of dashes would
// otherwise reach argparse as a flag. Order ids always start alphanumeric, and
// the gate pattern is anchored to match, so this never parses as a decision.
test("an admin cv approve with a flag-shaped order id is not treated as a command", async () => {
  await withAdminMode("true", async () => {
    const handler = cvHandler({ whatsappAdminUsers: [ADMIN] });
    const reply = await handler(eventFor("cv approve --order-x", ADMIN), {
      channelId: "whatsapp",
      senderId: ADMIN,
    });

    assert.deepEqual(handler.cvCalls(), []);
    assert.doesNotMatch(reply?.text ?? "", /approved|denied/i);
  });
});

test("an admin cv command is not honored while WhatsApp admin mode is disabled", async () => {
  await withAdminMode("false", async () => {
    const handler = cvHandler({ whatsappAdminUsers: [ADMIN] });
    const reply = await handler(eventFor(`cv approve ${CV_ORDER_ID}`, ADMIN), {
      channelId: "whatsapp",
      senderId: ADMIN,
    });

    assert.equal(reply, undefined);
    assert.deepEqual(handler.cvCalls(), []);
  });
});

test("a DM carrying a promo ad ref is counted without changing the reply", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-adref-"));
  const serviceStatePath = path.join(dir, "state.json");
  const handler = registerHandler({ serviceStatePath });
  const control = registerHandler({});
  const ctx = { channelId: "whatsapp", senderId: MEMBER };

  const reply = await handler(eventFor("Hi, I saw your borehole ad (ref BH-7K2)", MEMBER), ctx);
  const baseline = await control(eventFor("Hi, I saw your borehole ad", MEMBER), ctx);
  assert.deepEqual(reply, baseline);

  const { refs } = JSON.parse(fs.readFileSync(path.join(dir, "ad-refs.json"), "utf8"));
  assert.equal(refs["BH-7K2"].count, 1);
});

test("ad refs in group messages and admin DMs are not counted", async () => {
  await withAdminMode("true", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-adref-"));
    const serviceStatePath = path.join(dir, "state.json");
    const handler = registerHandler({ serviceStatePath, whatsappAdminUsers: [ADMIN] });

    const group = await handler(
      { ...eventFor("ref BH-7K2", MEMBER), isGroup: true },
      { channelId: "whatsapp", senderId: MEMBER },
    );
    assert.deepEqual(group, { handled: true });
    await handler(eventFor("ref BH-7K2", ADMIN), { channelId: "whatsapp", senderId: ADMIN });
    assert.equal(fs.existsSync(path.join(dir, "ad-refs.json")), false);
  });
});

// ---------------------------------------------------------------------------
// Sibling-line guard: the bot's two WhatsApp lines may converse, on a budget.
// ---------------------------------------------------------------------------

const LINE_DEFAULT = "+263785632943";
const LINE_ADS = "+263790453292";
const LINES = { default: LINE_DEFAULT, ads: LINE_ADS };

// A DM from the "default" line arriving on the "ads" line.
const crossLineCtx = { channelId: "whatsapp", senderId: LINE_DEFAULT, accountId: "ads" };

test("a cross-line DM under budget is answered exactly like any other DM", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-sibling-"));
  const handler = registerHandler({
    serviceStatePath: path.join(dir, "state.json"),
    whatsappLines: LINES,
  });
  const control = registerHandler({});

  const reply = await handler(eventFor("how are you holding up?", LINE_DEFAULT), crossLineCtx);
  const baseline = await control(eventFor("how are you holding up?", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });
  assert.deepEqual(reply, baseline, "the two lines should be free to talk below the budget");

  // A sibling line is not a customer, so its refs are not counted as ad replies.
  await handler(eventFor("ref BH-7K2", LINE_DEFAULT), crossLineCtx);
  assert.equal(fs.existsSync(path.join(dir, "ad-refs.json")), false);
});

test("a cross-line DM over budget is held silently", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-sibling-"));
  const handler = registerHandler({
    serviceStatePath: path.join(dir, "state.json"),
    whatsappLines: LINES,
    siblingLineMaxEventsPerWindow: 3,
  });

  for (let i = 0; i < 3; i += 1) {
    const reply = await handler(eventFor(`chat ${i}`, LINE_DEFAULT), crossLineCtx);
    assert.notDeepEqual(reply, { handled: true }, `message ${i + 1} should still get through`);
  }
  const held = await handler(eventFor("chat 4", LINE_DEFAULT), crossLineCtx);
  assert.deepEqual(held, { handled: true }, "over budget the message is claimed with no reply");

  const { pairs } = JSON.parse(fs.readFileSync(path.join(dir, "sibling-lines.json"), "utf8"));
  const entry = Object.values(pairs)[0];
  assert.equal(entry.totalMessages, 3);
  assert.equal(entry.totalSuppressed, 1);
});

test("admin self-chat is never held, however much the admin says", async () => {
  await withAdminMode("true", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-sibling-"));
    const handler = registerHandler({
      serviceStatePath: path.join(dir, "state.json"),
      whatsappLines: LINES,
      whatsappAdminUsers: [LINE_DEFAULT],
      siblingLineMaxEventsPerWindow: 2,
    });
    // the operator messaging the bot on its own number, on that same line
    const selfCtx = { channelId: "whatsapp", senderId: LINE_DEFAULT, accountId: "default" };
    for (let i = 0; i < 6; i += 1) {
      const reply = await handler(eventFor(`admin note ${i}`, LINE_DEFAULT), selfCtx);
      assert.notDeepEqual(reply, { handled: true }, `self-chat message ${i + 1} must not be held`);
    }
    assert.equal(fs.existsSync(path.join(dir, "sibling-lines.json")), false);
  });
});

test("one line cannot run the other line's admin commands", async () => {
  await withAdminMode("true", async () => {
    const handler = registerHandler({
      whatsappLines: LINES,
      whatsappAdminUsers: [LINE_DEFAULT],
    });
    const command = `allow ${UNKNOWN}`;
    // Same sender, same text: as admin self-chat it is an operator command, but
    // arriving on the OTHER line it must not be, since the admin number is also
    // the "default" line's number.
    const asAdmin = await handler(eventFor(command, LINE_DEFAULT), {
      channelId: "whatsapp",
      senderId: LINE_DEFAULT,
      accountId: "default",
    });
    const asSibling = await handler(eventFor(command, LINE_DEFAULT), crossLineCtx);
    assert.notDeepEqual(asSibling, asAdmin, "a sibling line must not be treated as the operator");
  });
});

test("a customer DM is never touched by the sibling-line guard", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-sibling-"));
  const handler = registerHandler({
    serviceStatePath: path.join(dir, "state.json"),
    whatsappLines: LINES,
    siblingLineMaxEventsPerWindow: 1,
  });
  const control = registerHandler({});
  for (let i = 0; i < 4; i += 1) {
    const reply = await handler(eventFor("hello", MEMBER), {
      channelId: "whatsapp",
      senderId: MEMBER,
      accountId: "ads",
    });
    const baseline = await control(eventFor("hello", MEMBER), { channelId: "whatsapp", senderId: MEMBER });
    assert.deepEqual(reply, baseline, `customer message ${i + 1} must be unaffected`);
  }
  assert.equal(fs.existsSync(path.join(dir, "sibling-lines.json")), false);
});

test("without an account id the guard stays out of the way", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-sibling-"));
  const handler = registerHandler({
    serviceStatePath: path.join(dir, "state.json"),
    whatsappLines: LINES,
    siblingLineMaxEventsPerWindow: 1,
  });
  const control = registerHandler({});
  // No accountId: self-chat and cross-line are indistinguishable, so fail open
  // rather than risk silencing the admin.
  for (let i = 0; i < 3; i += 1) {
    const reply = await handler(eventFor("hi", LINE_DEFAULT), { channelId: "whatsapp", senderId: LINE_DEFAULT });
    const baseline = await control(eventFor("hi", UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });
    assert.deepEqual(reply, baseline, `message ${i + 1} must fall through`);
  }
  assert.equal(fs.existsSync(path.join(dir, "sibling-lines.json")), false);
});

test('audit: CV tools and interview turns retain the authenticated receiving account',async()=>{
  const handler=cvHandler();
  const tool=handler.publicTool({agentId:'public',agentAccountId:'ads',requesterSenderId:UNKNOWN,messageChannel:'whatsapp'});
  await tool.execute('test',{action:'cv_maker_start'});
  assert.deepEqual(handler.cvCalls(),[`status --phone=${UNKNOWN} --account=ads`]);
  await handler(eventFor('Jane',UNKNOWN),{channelId:'whatsapp',senderId:UNKNOWN,accountId:'ads'});
  assert.ok(handler.cvCalls().includes(`interview-turn --phone=${UNKNOWN} --message=Jane --account=ads`));
  // A CV pointer from ads must not turn an unrelated main-line message into
  // an interview turn, nor change the original order's delivery account.
  const before=handler.cvCalls().length;
  await handler(eventFor('hello',UNKNOWN),{channelId:'whatsapp',senderId:UNKNOWN,accountId:'default'});
  assert.equal(handler.cvCalls().length,before);
});
