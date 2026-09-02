import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import plugin, { createLimiter, isQueueFullError, runJsonCommand } from "./index.js";
import { accountLinkRequiredText, VERBATIM_LINK_DIRECTIVE } from "./lib/account-link.js";
import { busyReply } from "./lib/runtime.js";
import { handleEventScoutRequest } from "./services/event-scout.js";
import { handleJobScoutRequest } from "./services/job-scout.js";

const MEMBER = "+233208864185";
const UNKNOWN = "+19995550123";
const ADMIN = "+213563719936";

// The real deployed greeting/menu, so intake tests assert the wired copy.
const GREETING_FILE = fileURLToPath(new URL("../../docs/public-greeting.txt", import.meta.url));
const MENU_PROMPT = /reply with a number/i;
const MENU_SERVICE = /Automatic Job & Internship searching/;
const HANDOFF = /a team member will contact you soon/i;

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
              jobScoutOnboardingEnabled: true,
              serviceStatePath,
              ...effectiveConfig,
            },
          },
          "whatsapp-relevance-gate": {
            config: {
              enabled: true,
              serviceKeywords: [
                "webetu",
                "meal",
                "reserve",
                "reservation",
                "news",
                "job",
                "event",
              ],
              serviceExamples: [
                "reserve a meal",
                "book a meal at webetu",
                "subscribe me to news",
                "find me a job",
                "any scholarships",
                "what events are near me",
              ],
              serviceSummary: "Webetu meals, personalized news, local events, and job scouting",
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
  dispatch.messageSending = async function messageSending(event, ctx) {
    for (const { name, callback } of handlers) {
      if (name !== "message_sending") continue;
      const reply = await callback(event, ctx);
      if (reply !== undefined) return reply;
    }
    return undefined;
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

test("disabled public services are omitted from the public service tool schema", async () => {
  const handler = registerHandler({ webetuEnabled: false, newsEnabled: false, eventScoutEnabled: false });
  const tool = handler.publicTool({ agentId: "public", requesterSenderId: MEMBER, messageChannel: "whatsapp" });

  assert.deepEqual(tool.parameters.properties.action.enum, ["job_scout_start_or_status"]);

  const result = await tool.execute("test", { action: "webetu_start" });
  assert.match(result.content[0].text, /isn't available here right now/i);
  assert.match(result.content[0].text, /Job Scout/);
});

test("classic mode: member disabled-service requests return unavailable", async () => {
  // Classic allowlist mode (gate off) is where members still reach the agent, so
  // the disabled-service guard only fires there now.
  const handler = registerHandler({
    memberGateEnabled: false,
    webetuEnabled: false,
    newsEnabled: false,
    eventScoutEnabled: false,
  });

  for (const text of [
    "webetu",
    "start personalized news",
    "setup event scouting",
    "add this event to my calendar",
  ]) {
    const memberReply = await handler(eventFor(text, MEMBER), {
      channelId: "whatsapp",
      senderId: MEMBER,
    });
    assert.equal(memberReply.handled, true, `expected member "${text}" to be handled`);
    assert.match(memberReply.text, /isn't available here right now/i, `expected member "${text}" to be unavailable`);
    assert.doesNotMatch(memberReply.text, /isn't open for new users/i, `expected "${text}" not to get the access-required reply`);
  }
});

test("classic mode: member Job Scout messages with stray disabled-service keywords reach the agent", async () => {
  const handler = registerHandler({ memberGateEnabled: false, webetuEnabled: false, newsEnabled: false, eventScoutEnabled: false });

  for (const text of [
    "any news on my job applications?",
    "find me a job in a restaurant",
  ]) {
    const reply = await handler(eventFor(text, MEMBER), {
      channelId: "whatsapp",
      senderId: MEMBER,
    });
    assert.equal(reply, undefined, `expected "${text}" to defer to the agent`);
  }
});

test("job scout onboarding has a generic disabled response", async () => {
  const reply = await handleJobScoutRequest({ pluginConfig: { jobScoutOnboardingEnabled: false } }, UNKNOWN);
  assert.equal(reply.handled, true);
  assert.match(reply.text, /temporarily unavailable/i);
  assert.doesNotMatch(reply.text, /pilot/i);
});

test("enabling the CV maker exposes cv_maker_start and routes to the intro", async () => {
  const handler = registerHandler({ cvMakerEnabled: true });
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

test("job scout asks for web or WhatsApp before creating a link", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-jobs-"));
  const accountLinkScript = path.join(dir, "account-link");
  const jobScoutScript = path.join(dir, "job-scout");
  const serviceStatePath = path.join(dir, "state.json");
  const helperLog = path.join(dir, "helper.log");
  writeExecutable(
    jobScoutScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":false,"ready":false,"onboardingChannel":null,"onboardingNextStep":"channel_selection"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${helperLog}"
printf '{"linked":false,"channel":"web","nextStep":"whatsapp","setupUrl":"https://app.example.test/whatsapp?token=abc"}\\n'
`,
  );
  const api = {
    config: {
      plugins: {
        entries: {
          "public-service-gate": {
            config: {
              enabled: true,
              accountLinkScript,
              jobScoutScript,
              serviceStatePath,
              jobScoutOnboardingEnabled: true,
            },
          },
        },
      },
    },
    logger: { debug() {}, warn() {} },
  };

  const firstReply = await handleJobScoutRequest(api, MEMBER, "Set up Job Scout");
  assert.equal(firstReply.handled, true);
  assert.match(firstReply.text, /1\. In the web app/);
  assert.match(firstReply.text, /2\. Here in WhatsApp/);
  assert.doesNotMatch(firstReply.text, /https?:\/\//);
  assert.equal(fs.existsSync(helperLog), false);

  const secondReply = await handleJobScoutRequest(api, MEMBER, "web");
  assert.match(secondReply.text, /Finish setting up Job Scout in the web app/);
  assert.match(secondReply.text, /https:\/\/app\.example\.test\/whatsapp\?token=abc/);
  assert.match(fs.readFileSync(helperLog, "utf8"), /start-onboarding --phone \+233208864185 --service jobs --channel web/);
});

test("job scout chat selection creates the link only after the choice", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-jobs-"));
  const accountLinkScript = path.join(dir, "account-link");
  const jobScoutScript = path.join(dir, "job-scout");
  const serviceStatePath = path.join(dir, "state.json");
  writeExecutable(
    jobScoutScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":false,"ready":false,"onboardingChannel":null}\\n' ;;
  *) exit 2 ;;
esac
`,
  );
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
if [ "$1" != "start-onboarding" ] || [ "$7" != "chat" ]; then exit 2; fi
printf '{"linked":false,"channel":"chat","nextStep":"whatsapp","setupUrl":"https://app.example.test/whatsapp?token=chat"}\\n'
`,
  );
  const api = {
    config: {
      plugins: {
        entries: {
          "public-service-gate": {
            config: {
              enabled: true,
              accountLinkScript,
              jobScoutScript,
              serviceStatePath,
              jobScoutOnboardingEnabled: true,
            },
          },
        },
      },
    },
    logger: { debug() {}, warn() {} },
  };

  const firstReply = await handleJobScoutRequest(api, MEMBER, "Set up Job Scout");
  assert.doesNotMatch(firstReply.text, /https?:\/\//);
  const reply = await handleJobScoutRequest(api, MEMBER, "2");
  assert.equal(reply.handled, true);
  assert.match(reply.text, /Sign in to connect your account here/);
  assert.match(reply.text, /Gmail is granted as part of sign-in/);
  assert.match(reply.text, /app will bring you back here automatically/);
  assert.match(reply.text, /https:\/\/app\.example\.test\/whatsapp\?token=chat/);
});

test("public service forwards the user's Job Scout channel choice", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-tool-jobs-"));
  const accountLinkScript = path.join(dir, "account-link");
  const jobScoutScript = path.join(dir, "job-scout");
  writeExecutable(
    jobScoutScript,
    `#!/usr/bin/env bash
printf '{"linked":false,"ready":false,"onboardingChannel":null}\\n'
`,
  );
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
printf '{"linked":false,"channel":"web","nextStep":"whatsapp","setupUrl":"https://app.example.test/whatsapp?token=tool"}\\n'
`,
  );
  const handler = registerHandler({ accountLinkScript, jobScoutScript });
  const tool = handler.publicTool({ agentId: "public", requesterSenderId: MEMBER, messageChannel: "whatsapp" });

  const result = await tool.execute("test", { action: "job_scout_start_or_status", text: "web" });
  assert.match(result.content[0].text, /whatsapp\?token=tool/);
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
  writeServiceState(serviceStatePath, MEMBER, "webetu", "webetu.awaiting_restaurant");

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
  writeServiceState(serviceStatePath, MEMBER, "webetu", "webetu.awaiting_restaurant");

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
    { to: MEMBER, content: "Your default restaurant is Test Residence." },
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

test("unknown sender first message shows the service menu without an LLM call", async () => {
  let calls = 0;
  const handler = registerHandler({ greetingTemplatePath: GREETING_FILE }, async () => {
    calls += 1;
    return "IRRELEVANT";
  });
  const reply = await handler(eventFor("hi", UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });

  assert.equal(reply.handled, true);
  assert.match(reply.text, MENU_PROMPT);
  assert.match(reply.text, MENU_SERVICE);
  assert.doesNotMatch(reply.text, /waiting for review/i);
  assert.equal(calls, 0);
});

test("unknown sender service keyword also lands on the menu", async () => {
  let calls = 0;
  const handler = registerHandler({ greetingTemplatePath: GREETING_FILE }, async () => {
    calls += 1;
    return "IRRELEVANT";
  });
  const reply = await handler(eventFor("webetu", UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });

  assert.equal(reply.handled, true);
  assert.match(reply.text, MENU_PROMPT);
  assert.equal(calls, 0);
});

test("unknown sender unrelated text lands on the menu instead of silence", async () => {
  let calls = 0;
  const handler = registerHandler({ greetingTemplatePath: GREETING_FILE }, async () => {
    calls += 1;
    return "IRRELEVANT";
  });
  const reply = await handler(eventFor("tell me the bitcoin price now", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.match(reply.text, MENU_PROMPT);
  assert.equal(calls, 0);
});

test("first-contact lead is recorded via the member store with the message snippet", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-lead-"));
  const memberStoreScript = path.join(dir, "whatsapp-invite");
  const argsLog = path.join(dir, "member-args.txt");
  writeExecutable(
    memberStoreScript,
    `#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "${argsLog}"
case "$1" in
  status) printf '{"phone":"%s","status":"unknown","unlocked":false}\\n' "$3" ;;
  request) printf '{"ok":true,"phone":"%s","status":"pending","changed":true,"isNew":true}\\n' "$3" ;;
  *) exit 2 ;;
esac
`,
  );

  // No whatsappAdminUsers configured -> notifyAdminOfLead short-circuits (no send).
  const handler = registerHandler({ memberStoreScript, greetingTemplatePath: GREETING_FILE });
  const reply = await handler(eventFor("hello I'm interested how do I apply", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.match(reply.text, MENU_PROMPT);
  const logged = fs.readFileSync(argsLog, "utf-8");
  assert.match(logged, /request --phone \+19995550123 --message hello I'm interested how do I apply/);
});

test("join codes are no longer special and just land on the menu", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const memberStoreScript = path.join(dir, "whatsapp-invite");
  const argsLog = path.join(dir, "member-args.txt");
  writeExecutable(
    memberStoreScript,
    `#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "${argsLog}"
case "$1" in
  status) printf '{"phone":"%s","status":"unknown","unlocked":false}\\n' "$3" ;;
  request) printf '{"ok":true,"phone":"%s","status":"pending","changed":true,"isNew":true}\\n' "$3" ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ memberStoreScript, greetingTemplatePath: GREETING_FILE }, async () => "RELEVANT");
  const reply = await handler(eventFor("JOIN-ABC123", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.match(reply.text ?? "", MENU_PROMPT);
  assert.doesNotMatch(fs.readFileSync(argsLog, "utf-8"), /redeem/);
});

function intakeStore(argsLog) {
  return `#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "${argsLog}"
case "$1" in
  status) printf '{"phone":"%s","status":"unknown","unlocked":false}\\n' "$3" ;;
  request) printf '{"ok":true,"phone":"%s","status":"pending","changed":true,"isNew":true}\\n' "$3" ;;
  *) exit 2 ;;
esac
`;
}

test("menu selection captures the request under the chosen service and hands off", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-intake-"));
  const memberStoreScript = path.join(dir, "whatsapp-invite");
  const argsLog = path.join(dir, "member-args.txt");
  const serviceStatePath = path.join(dir, "state.json");
  writeExecutable(memberStoreScript, intakeStore(argsLog));

  const handler = registerHandler({ memberStoreScript, serviceStatePath, greetingTemplatePath: GREETING_FILE });
  const ctx = { channelId: "whatsapp", senderId: UNKNOWN };

  const menu = await handler(eventFor("hi", UNKNOWN), ctx);
  assert.match(menu.text, MENU_PROMPT);

  const done = await handler(eventFor("2", UNKNOWN), ctx);
  assert.equal(done.handled, true);
  assert.match(done.text, HANDOFF);

  // The second turn records the picked service so the admin sees it in the store.
  assert.match(fs.readFileSync(argsLog, "utf-8"), /request --phone \+19995550123 --message \[CV writing & revamping\] 2/);
  // State is cleared once the order is captured.
  const saved = JSON.parse(fs.readFileSync(serviceStatePath, "utf8"));
  assert.deepEqual(saved.states, {});
});

test("free-text request is captured under the matched service label", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-intake-"));
  const memberStoreScript = path.join(dir, "whatsapp-invite");
  const argsLog = path.join(dir, "member-args.txt");
  const serviceStatePath = path.join(dir, "state.json");
  writeExecutable(memberStoreScript, intakeStore(argsLog));

  const handler = registerHandler({ memberStoreScript, serviceStatePath, greetingTemplatePath: GREETING_FILE });
  const ctx = { channelId: "whatsapp", senderId: UNKNOWN };

  await handler(eventFor("hi", UNKNOWN), ctx);
  const done = await handler(eventFor("find me a job please", UNKNOWN), ctx);

  assert.match(done.text, HANDOFF);
  assert.match(
    fs.readFileSync(argsLog, "utf-8"),
    /request --phone \+19995550123 --message \[Automatic Job & Internship searching\] find me a job please/,
  );
});

test("greeting again re-shows the menu instead of completing the order", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-intake-"));
  const serviceStatePath = path.join(dir, "state.json");

  const handler = registerHandler({ serviceStatePath, greetingTemplatePath: GREETING_FILE });
  const ctx = { channelId: "whatsapp", senderId: UNKNOWN };

  await handler(eventFor("hi", UNKNOWN), ctx);
  const again = await handler(eventFor("menu", UNKNOWN), ctx);

  assert.match(again.text, MENU_PROMPT);
  assert.doesNotMatch(again.text, HANDOFF);
  // Still awaiting a selection, not cleared.
  const saved = JSON.parse(fs.readFileSync(serviceStatePath, "utf8"));
  assert.equal(saved.states[UNKNOWN]?.step, "intake.awaiting_selection");
});

test("invite link requests are no longer handled deterministically", async () => {
  const handler = registerHandler({ botWhatsappNumber: "+263785632943" }, async () => "IRRELEVANT");
  const reply = await handler(eventFor("invite link", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.doesNotMatch(reply.text ?? "", /wa\.me|JOIN-/);
});

test("classic mode: member greeting falls through to the agent", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const greetingTemplatePath = path.join(dir, "public-greeting.txt");
  fs.writeFileSync(greetingTemplatePath, "Choose a service");

  const handler = registerHandler({ memberGateEnabled: false, greetingTemplatePath });
  const reply = await handler(eventFor("hi"), { channelId: "whatsapp", senderId: MEMBER });

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
  const accountLinkScript = path.join(dir, "account-link");
  const jobScoutScript = path.join(dir, "job-scout");
  const newsSubscriptionsScript = path.join(dir, "news_subscriptions.py");
  const helperLog = path.join(dir, "helper.log");

  for (const script of [accountLinkScript, jobScoutScript]) {
    writeExecutable(
      script,
      `#!/usr/bin/env bash
printf '%s %s\\n' "$0" "$*" >> "${helperLog}"
exit 2
`,
    );
  }
  fs.writeFileSync(
    newsSubscriptionsScript,
    `#!/usr/bin/env python3
from pathlib import Path
Path(${JSON.stringify(helperLog)}).write_text("news helper called", encoding="utf-8")
raise SystemExit(2)
`,
    { mode: 0o755 },
  );

  const handler = registerHandler({ memberGateEnabled: false, accountLinkScript, jobScoutScript, newsSubscriptionsScript });
  for (const text of [
    "Hello",
    "1",
    "2",
    "3",
    "4",
    "Done",
    "I'm done saving credentials for webetu",
    "start webetu",
    "visual guide for webetu",
    "check my reservation status",
    "stop news",
    "personalized news about AI",
    "setup event scouting",
    "setup job scout",
  ]) {
    const reply = await handler(eventFor(text), { channelId: "whatsapp", senderId: MEMBER });
    assert.equal(reply, undefined, `expected "${text}" to reach the agent`);
  }
  assert.equal(fs.existsSync(helperLog), false);
});

test("event scout setup copy promises user selection before calendar saves", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-events-"));
  const accountLinkScript = path.join(dir, "account-link");
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status)
    printf '{"linked":true,"publicUserId":"public-123"}\\n'
    ;;
  *) exit 2 ;;
esac
`,
  );
  const api = {
    config: {
      plugins: {
        entries: {
          "public-service-gate": {
            config: {
              enabled: true,
              accountLinkScript,
              agentGenaiePublicBaseUrl: "https://app.example.test",
            },
          },
        },
      },
    },
    logger: {
      warn() {},
    },
  };

  const reply = await handleEventScoutRequest(api, MEMBER);

  assert.equal(reply.handled, true);
  assert.match(reply.text, /send matching events here/i);
  assert.match(reply.text, /pick which ones go into your calendar/i);
  assert.doesNotMatch(reply.text, /I'll add matching upcoming events/i);
});

test("classic mode: active service state no longer short-circuits member continuations", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const serviceStatePath = path.join(dir, "state.json");
  writeServiceState(serviceStatePath, MEMBER, "webetu", "webetu.awaiting_restaurant");

  const handler = registerHandler({ memberGateEnabled: false, serviceStatePath });
  for (const text of ["2", "menu", "Residence Test Restaurant", "RU 2000 Lits Tipaza"]) {
    const reply = await handler(eventFor(text), { channelId: "whatsapp", senderId: MEMBER });
    assert.equal(reply, undefined, `expected "${text}" to reach the agent`);
  }
  const saved = JSON.parse(fs.readFileSync(serviceStatePath, "utf8"));
  assert.equal(saved.states[MEMBER]?.step, "webetu.awaiting_restaurant");
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

test("admin sender follows the public intake when WhatsApp admin mode is disabled", async () => {
  await withAdminMode("false", async () => {
    const handler = registerHandler({ whatsappAdminUsers: [ADMIN], greetingTemplatePath: GREETING_FILE });
    const reply = await handler(eventFor("hi", ADMIN), { channelId: "whatsapp", senderId: ADMIN });

    assert.equal(reply.handled, true);
    assert.match(reply.text, MENU_PROMPT);
  });
});

test("admin allow command unlocks the target and is claimed", async () => {
  await withAdminMode("true", async () => {
    for (const command of ["allow", "grandfather"]) {
      const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
      const memberStoreScript = path.join(dir, "whatsapp-invite");
      const argsLog = path.join(dir, "allow-args.txt");
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

      const handler = registerHandler({ whatsappAdminUsers: [ADMIN], memberStoreScript });
      const reply = await handler(eventFor(`${command} +263788390903`, ADMIN), {
        channelId: "whatsapp",
        senderId: ADMIN,
      });

      assert.equal(reply.handled, true, `expected "${command}" to be claimed`);
      assert.match(reply.text, /Allowed \+263788390903: unlocked/);
      assert.equal(fs.readFileSync(argsLog, "utf-8").trim(), "grandfather --phone +263788390903");
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

    assert.equal(reply.handled, true);
    assert.doesNotMatch(reply.text ?? "", /Allowed/);
    assert.equal(fs.existsSync(argsLog), false);
  });
});

test("non-admin allow command does not unlock and gets gated", async () => {
  let calls = 0;
  const handler = registerHandler({ whatsappAdminUsers: [ADMIN] }, async () => {
    calls += 1;
    return "IRRELEVANT";
  });
  const reply = await handler(eventFor("allow +263788390903", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.doesNotMatch(reply.text ?? "", /Allowed/);
});

test("runJsonCommand parses JSON stdout", async () => {
  const result = await runJsonCommand("/bin/echo", ['{"status":"ok","count":2}']);

  assert.deepEqual(result, { status: "ok", count: 2 });
});

test("public canned replies use the concise style", () => {
  assert.equal(
    accountLinkRequiredText("https://example.test/setup", "Event scouting"),
    `${VERBATIM_LINK_DIRECTIVE}\n\nSign in here to set up Event scouting:\nhttps://example.test/setup\n\nCheck Spam if the sign-in email doesn't arrive.`,
  );
  assert.equal(busyReply().text, "I'm busy right now. Try again shortly.");

  const files = [
    "lib/account-link.js",
    "lib/runtime.js",
    "services/membership.js",
    "services/greeting.js",
    "services/intake.js",
    "services/webetu.js",
    "services/news.js",
    "services/event-scout.js",
    "services/job-scout.js",
    "services/cv-maker.js",
  ];
  for (const file of files) {
    const source = fs.readFileSync(new URL(file, import.meta.url), "utf8");
    assert.doesNotMatch(source, /\u2014/, `${file} contains an em dash`);
  }

  const docs = [
    "public-whatsapp-system-prompt.txt",
    "public-services.md",
    "public-greeting.txt",
    "calendar-save-onboarding.md",
    "event-scout-onboarding.md",
    "gmail-onboarding.md",
    "job-scout-onboarding.md",
    "personalized-news-onboarding.md",
    "webetu-onboarding.md",
  ];
  for (const file of docs) {
    const source = fs.readFileSync(new URL(`../../docs/${file}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /\u2014/, `${file} contains an em dash`);
  }

  const prompt = fs.readFileSync(
    new URL("../../docs/public-whatsapp-system-prompt.txt", import.meta.url),
    "utf8",
  );
  assert.match(prompt, /use 1-2 short sentences by default/i);
  assert.match(prompt, /Do not use em dashes/i);
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
// Paid CV order flow (menu option 2)
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
  interview-turn)
    if [ "$5" = "done" ]; then
      printf '{"ok":true,"orderId":"${CV_ORDER_ID}","status":"awaiting_payment","reply":"All set.","complete":true}\\n'
    else
      printf '{"ok":true,"orderId":"${CV_ORDER_ID}","status":"interviewing","reply":"What is your full name?","complete":false}\\n'
    fi
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
    greetingTemplatePath: GREETING_FILE,
    ...extraConfig,
  });
  handler.cvCalls = cli.calls;
  handler.serviceStatePath = serviceStatePath;
  return handler;
}

test("menu option 2 branches into the automated CV flow instead of the manual handoff", async () => {
  const handler = cvHandler();
  const ctx = { channelId: "whatsapp", senderId: UNKNOWN };

  const menu = await handler(eventFor("hi", UNKNOWN), ctx);
  assert.match(menu.text, MENU_PROMPT);

  const started = await handler(eventFor("2", UNKNOWN), ctx);
  assert.equal(started.handled, true);
  assert.doesNotMatch(started.text, HANDOFF);
  assert.match(started.text, /CV/);
  // The order itself is created by the first relayed turn, not by the digit.
  assert.deepEqual(handler.cvCalls(), []);
  const saved = JSON.parse(fs.readFileSync(handler.serviceStatePath, "utf8"));
  assert.equal(saved.states[UNKNOWN]?.service, "cv");
});

test("a free-text CV request also branches into the CV flow", async () => {
  const handler = cvHandler();
  const ctx = { channelId: "whatsapp", senderId: UNKNOWN };

  await handler(eventFor("hi", UNKNOWN), ctx);
  const started = await handler(eventFor("please revamp my cv", UNKNOWN), ctx);

  assert.doesNotMatch(started.text, HANDOFF);
  const saved = JSON.parse(fs.readFileSync(handler.serviceStatePath, "utf8"));
  assert.equal(saved.states[UNKNOWN]?.service, "cv");
});

test("menu options 1, 3 and 4 keep the manual handoff while the CV flow is live", async () => {
  for (const digit of ["1", "3", "4"]) {
    const handler = cvHandler();
    const ctx = { channelId: "whatsapp", senderId: UNKNOWN };

    await handler(eventFor("hi", UNKNOWN), ctx);
    const done = await handler(eventFor(digit, UNKNOWN), ctx);

    assert.equal(done.handled, true, `expected "${digit}" to be handled`);
    assert.match(done.text, HANDOFF, `expected "${digit}" to hand off`);
    assert.deepEqual(handler.cvCalls(), [], `expected "${digit}" not to touch the CV maker`);
    // State is cleared once the manual order is captured, exactly as before.
    const saved = JSON.parse(fs.readFileSync(handler.serviceStatePath, "utf8"));
    assert.deepEqual(saved.states, {});
  }
});

test("menu option 2 still hands off while the CV maker is disabled", async () => {
  const handler = cvHandler({ cvMakerEnabled: false });
  const ctx = { channelId: "whatsapp", senderId: UNKNOWN };

  await handler(eventFor("hi", UNKNOWN), ctx);
  const done = await handler(eventFor("2", UNKNOWN), ctx);

  assert.match(done.text, HANDOFF);
  assert.deepEqual(handler.cvCalls(), []);
});

test("CV flow turns relay to the CLI and the payment message uses the configured values", async () => {
  const handler = cvHandler({
    cvMakerPriceText: "US$9",
    cvMakerPaymentDetails: "TestCash 0000 (Test Payee)",
  });
  const ctx = { channelId: "whatsapp", senderId: UNKNOWN };

  await handler(eventFor("hi", UNKNOWN), ctx);
  await handler(eventFor("2", UNKNOWN), ctx);

  const turn = await handler(eventFor("Jane", UNKNOWN), ctx);
  assert.equal(turn.text, "What is your full name?");

  const priced = await handler(eventFor("done", UNKNOWN), ctx);
  assert.match(priced.text, /All set\./);
  assert.match(priced.text, /US\$9/);
  assert.match(priced.text, /TestCash 0000 \(Test Payee\)/);

  const paid = await handler(eventFor("paid", UNKNOWN), ctx);
  assert.match(paid.text, /confirming your payment/i);

  assert.deepEqual(handler.cvCalls(), [
    `interview-turn --phone ${UNKNOWN} --message Jane`,
    `interview-turn --phone ${UNKNOWN} --message done`,
    `paid --phone ${UNKNOWN}`,
  ]);
  const saved = JSON.parse(fs.readFileSync(handler.serviceStatePath, "utf8"));
  assert.equal(saved.states[UNKNOWN]?.step, "cv.awaiting_approval");
});

test("menu escapes the CV flow back to the service menu", async () => {
  const handler = cvHandler();
  const ctx = { channelId: "whatsapp", senderId: UNKNOWN };

  await handler(eventFor("hi", UNKNOWN), ctx);
  await handler(eventFor("2", UNKNOWN), ctx);
  const back = await handler(eventFor("menu", UNKNOWN), ctx);

  assert.match(back.text, MENU_PROMPT);
  assert.deepEqual(handler.cvCalls(), []);
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
      assert.deepEqual(handler.cvCalls(), [`${command} --order ${CV_ORDER_ID}`]);
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

    assert.equal(reply.handled, true);
    assert.doesNotMatch(reply.text ?? "", /approved|denied/i);
    assert.match(reply.text, MENU_PROMPT);
    assert.deepEqual(handler.cvCalls(), []);
  });
});

test("an admin cv command is not honored while WhatsApp admin mode is disabled", async () => {
  await withAdminMode("false", async () => {
    const handler = cvHandler({ whatsappAdminUsers: [ADMIN] });
    const reply = await handler(eventFor(`cv approve ${CV_ORDER_ID}`, ADMIN), {
      channelId: "whatsapp",
      senderId: ADMIN,
    });

    assert.equal(reply.handled, true);
    assert.doesNotMatch(reply.text ?? "", /approved/i);
    assert.deepEqual(handler.cvCalls(), []);
  });
});
