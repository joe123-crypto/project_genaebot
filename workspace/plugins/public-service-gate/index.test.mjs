import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import plugin, { createLimiter, inviteLinkText, isQueueFullError, runJsonCommand } from "./index.js";

const MEMBER = "+233208864185";
const UNKNOWN = "+19995550123";
const ADMIN = "+213563719936";

function registerHandler(config = {}, complete, relevanceConfig = {}) {
  const handlers = [];
  const serviceStatePath =
    config.serviceStatePath ?? path.join(fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-state-")), "state.json");
  let effectiveConfig = config;
  if (!effectiveConfig.inviteScript) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-invite-"));
    const inviteScript = path.join(dir, "whatsapp-invite");
    writeExecutable(
      inviteScript,
      `#!/usr/bin/env bash
set -eu
case "$1" in
  status)
    if [ "$3" = "${MEMBER}" ]; then
      printf '{"phone":"${MEMBER}","status":"unlocked","pending":false,"unlocked":true}\\n'
    else
      printf '{"phone":"%s","status":"unknown","pending":false,"unlocked":false}\\n' "$3"
    fi
    ;;
  *) exit 2 ;;
esac
`,
    );
    effectiveConfig = { ...effectiveConfig, inviteScript };
  }
  const api = {
    config: {
      plugins: {
        entries: {
          "public-service-gate": {
            config: {
              enabled: true,
              publicUsers: [MEMBER],
              invitesEnabled: true,
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
                "invite",
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
  return dispatch;
}

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

test("failsafe stop command claims unknown sender before invite or LLM checks", async () => {
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

test("failsafe stop command requires a one-word stop message", async () => {
  const handler = registerHandler({});

  assert.equal(await handler(eventFor("please stop"), { channelId: "whatsapp", senderId: MEMBER }), undefined);
  assert.equal(await handler(eventFor("don't stop"), { channelId: "whatsapp", senderId: MEMBER }), undefined);
  assert.equal(await handler(eventFor("unstoppable"), { channelId: "whatsapp", senderId: MEMBER }), undefined);
});

test("stop news still follows the personalized-news stop path", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const newsSubscriptionsScript = path.join(dir, "news_subscriptions.py");
  const newsLog = path.join(dir, "news.log");
  fs.writeFileSync(
    newsSubscriptionsScript,
    `#!/usr/bin/env python3
import json
import sys
from pathlib import Path
Path(${JSON.stringify(newsLog)}).write_text(" ".join(sys.argv[1:]), encoding="utf-8")
print(json.dumps({"ok": True}))
`,
    { mode: 0o755 },
  );

  const handler = registerHandler({ newsSubscriptionsScript });
  const reply = await handler(eventFor("stop news"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /Personalized news has been stopped/);
  assert.equal(fs.readFileSync(newsLog, "utf8"), "disable --phone +233208864185");
});

test("unknown sender greeting gets invite-required reply instead of silence", async () => {
  let calls = 0;
  const handler = registerHandler({}, async () => {
    calls += 1;
    return "IRRELEVANT";
  });
  const reply = await handler(eventFor("hi", UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /invite-only/);
  assert.match(reply.text, /invite link/);
  assert.equal(calls, 0);
});

test("unknown sender service keyword gets invite-required reply", async () => {
  let calls = 0;
  const handler = registerHandler({}, async () => {
    calls += 1;
    return "IRRELEVANT";
  });
  const reply = await handler(eventFor("webetu", UNKNOWN), { channelId: "whatsapp", senderId: UNKNOWN });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /invite-only/);
  assert.equal(calls, 0);
});

test("unknown sender unrelated text remains a silent handled drop", async () => {
  let calls = 0;
  const handler = registerHandler({}, async () => {
    calls += 1;
    return "IRRELEVANT";
  });
  const reply = await handler(eventFor("tell me the bitcoin price now", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.equal(reply.text, undefined);
  assert.equal(calls, 1);
});

test("unknown sender valid join code redeems and returns pending activation instructions", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const inviteScript = path.join(dir, "whatsapp-invite");
  const publicUserScript = path.join(dir, "whatsapp-public-user.py");
  fs.writeFileSync(publicUserScript, "import sys\n");
  writeExecutable(
    inviteScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  redeem) printf '{"ok":true,"inviter":"${MEMBER}"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ inviteScript, publicUserScript });
  const reply = await handler(eventFor("JOIN-ABC123", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /not unlocked yet/);
  assert.match(reply.text, /Type "invite link"/);
  assert.doesNotMatch(reply.text, /welcome to OpenClaw Service Desk/);
});

test("unknown sender invalid join code still returns invalid invite reply", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const inviteScript = path.join(dir, "whatsapp-invite");
  writeExecutable(
    inviteScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  redeem) printf '{"ok":false}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ inviteScript });
  const reply = await handler(eventFor("JOIN-DEAD00", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /invalid or has already been used/);
});

test("pending invite member gets activation instructions instead of service menu", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const inviteScript = path.join(dir, "whatsapp-invite");
  const greetingTemplatePath = path.join(dir, "public-greeting.txt");
  fs.writeFileSync(greetingTemplatePath, "Choose a service");
  writeExecutable(
    inviteScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"phone":"+19995550123","status":"pending","pending":true,"unlocked":false}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ inviteScript, greetingTemplatePath });
  const reply = await handler(eventFor("hi", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /not unlocked yet/);
  assert.match(reply.text, /Type "invite link"/);
  assert.doesNotMatch(reply.text, /Choose a service/);
});

test("pending invite member in publicUsers is still blocked from service options", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const inviteScript = path.join(dir, "whatsapp-invite");
  const accountLinkScript = path.join(dir, "account-link");
  writeExecutable(
    inviteScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"phone":"+19995550123","status":"pending","pending":true,"unlocked":false}\\n' ;;
  *) exit 2 ;;
esac
`,
  );
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":false}\\n' ;;
  create-invite) printf '{"setupUrl":"https://example.test/setup?token=abc"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ inviteScript, accountLinkScript, publicUsers: [MEMBER, UNKNOWN] });
  const reply = await handler(eventFor("1", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /not unlocked yet/);
  assert.doesNotMatch(reply.text, /https:\/\/example\.test\/setup/);
});

test("pending invite member can request an invite link", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const inviteScript = path.join(dir, "whatsapp-invite");
  writeExecutable(
    inviteScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"phone":"+19995550123","status":"pending","pending":true,"unlocked":false}\\n' ;;
  create) printf '{"code":"PEND1234","inviter":"+19995550123","status":"created"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ inviteScript, botWhatsappNumber: "+263785632943" });
  const reply = await handler(eventFor("invite link", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /https:\/\/wa\.me\/263785632943\?text=JOIN-PEND1234/);
  assert.match(reply.text, /service access will unlock/);
});

test("unlocked invite member gets normal member handling without publicUsers entry", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const inviteScript = path.join(dir, "whatsapp-invite");
  const greetingTemplatePath = path.join(dir, "public-greeting.txt");
  fs.writeFileSync(greetingTemplatePath, "Choose a service");
  writeExecutable(
    inviteScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"phone":"+19995550123","status":"unlocked","pending":false,"unlocked":true}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ inviteScript, greetingTemplatePath });
  const reply = await handler(eventFor("hi", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.equal(reply.text, "Choose a service");
});

test("member greeting still gets the normal service menu", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const greetingTemplatePath = path.join(dir, "public-greeting.txt");
  fs.writeFileSync(greetingTemplatePath, "Choose a service");

  const handler = registerHandler({ greetingTemplatePath });
  const reply = await handler(eventFor("hi"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply.handled, true);
  assert.equal(reply.text, "Choose a service");
});

test("member off-scope text falls through to the relevance gate", async () => {
  let calls = 0;
  const handler = registerHandler(
    {},
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

test("member off-scope text falls through with nested live-style event fields", async () => {
  const handler = registerHandler({}, undefined, { rejectionMessage: "Please choose a supported service." });
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

test("admin sender follows public invite gate when WhatsApp admin mode is disabled", async () => {
  await withAdminMode("false", async () => {
    const handler = registerHandler({ whatsappAdminUsers: [ADMIN] });
    const reply = await handler(eventFor("hi", ADMIN), { channelId: "whatsapp", senderId: ADMIN });

    assert.equal(reply.handled, true);
    assert.match(reply.text, /invite-only/);
  });
});

test("admin grandfather command unlocks the target and is claimed", async () => {
  await withAdminMode("true", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
    const inviteScript = path.join(dir, "whatsapp-invite");
    const argsLog = path.join(dir, "grandfather-args.txt");
    writeExecutable(
      inviteScript,
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

    const handler = registerHandler({ whatsappAdminUsers: [ADMIN], inviteScript });
    const reply = await handler(eventFor("grandfather +263788390903", ADMIN), {
      channelId: "whatsapp",
      senderId: ADMIN,
    });

    assert.equal(reply.handled, true);
    assert.match(reply.text, /Grandfathered \+263788390903: unlocked/);
    assert.equal(fs.readFileSync(argsLog, "utf-8").trim(), "grandfather --phone +263788390903");
  });
});

test("admin grandfather command is not honored when WhatsApp admin mode is disabled", async () => {
  await withAdminMode("false", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
    const inviteScript = path.join(dir, "whatsapp-invite");
    const argsLog = path.join(dir, "grandfather-args.txt");
    writeExecutable(
      inviteScript,
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

    const handler = registerHandler({ whatsappAdminUsers: [ADMIN], inviteScript }, async () => "IRRELEVANT");
    const reply = await handler(eventFor("grandfather +263788390903", ADMIN), {
      channelId: "whatsapp",
      senderId: ADMIN,
    });

    assert.equal(reply.handled, true);
    assert.doesNotMatch(reply.text ?? "", /Grandfathered/);
    assert.equal(fs.existsSync(argsLog), false);
  });
});

test("non-admin grandfather command does not unlock and gets gated", async () => {
  let calls = 0;
  const handler = registerHandler({ whatsappAdminUsers: [ADMIN] }, async () => {
    calls += 1;
    return "IRRELEVANT";
  });
  const reply = await handler(eventFor("grandfather +263788390903", UNKNOWN), {
    channelId: "whatsapp",
    senderId: UNKNOWN,
  });

  assert.equal(reply.handled, true);
  assert.doesNotMatch(reply.text ?? "", /Grandfathered/);
});

test("menu option 1 starts Webetu setup instead of falling through", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":false}\\n' ;;
  create-invite) printf '{"setupUrl":"https://example.test/setup?token=abc"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ accountLinkScript });
  const reply = await handler(eventFor("1"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /https:\/\/example\.test\/setup/);
  assert.doesNotMatch(reply.text, /only help/i);
});

test("webetu guide request starts current setup flow without onboarding guide URL", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":false}\\n' ;;
  create-invite) printf '{"setupUrl":"https://example.test/setup?token=guide"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ accountLinkScript });
  const reply = await handler(eventFor("visual guide for webetu"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /https:\/\/example\.test\/setup/);
  assert.doesNotMatch(reply.text, /\/onboarding\b|Visual setup guide/i);
});

test("menu option 3 sends the account-link invite for event scouting when unlinked", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  const serviceStatePath = path.join(dir, "state.json");
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":false}\\n' ;;
  create-invite) printf '{"setupUrl":"https://example.test/setup?token=evt"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ accountLinkScript, serviceStatePath });
  const reply = await handler(eventFor("3"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /https:\/\/example\.test\/setup/);
  assert.doesNotMatch(reply.text, /not active|news pilot/i);
});

test("event scouting request starts setup with calendar connect link when linked", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  const serviceStatePath = path.join(dir, "state.json");
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":true,"publicUserId":"usr_test"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({
    accountLinkScript,
    serviceStatePath,
    agentGenaiePublicBaseUrl: "https://app.test",
  });
  const reply = await handler(eventFor("setup event scouting"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /https:\/\/app\.test\/usr_test\/connect-gmail/);
  assert.match(reply.text, /calendar/i);
});

test("setup/onboard phrasings claim event scouting deterministically", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  const serviceStatePath = path.join(dir, "state.json");
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":true,"publicUserId":"usr_test"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({
    accountLinkScript,
    serviceStatePath,
    agentGenaiePublicBaseUrl: "https://app.test",
  });

  for (const text of [
    "local event scouting setup",
    "onboard me for event scouting",
    "event scouting onboard",
  ]) {
    const reply = await handler(eventFor(text), { channelId: "whatsapp", senderId: MEMBER });
    assert.equal(reply?.handled, true, `expected "${text}" to be handled`);
    assert.match(reply.text, /https:\/\/app\.test\/usr_test\/connect-gmail/);
  }
});

test("event questions without a setup keyword fall through to the LLM", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  const serviceStatePath = path.join(dir, "state.json");
  // If the gate ever claims one of these it would call account-link; failing
  // here makes that mistake obvious instead of silently passing.
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
exit 2
`,
  );

  const handler = registerHandler({
    accountLinkScript,
    serviceStatePath,
    agentGenaiePublicBaseUrl: "https://app.test",
  });

  for (const text of [
    "start event scouting",
    "scout events for me",
    "find events near me",
    "Can I change the types of events that you scout for me?",
    "I mean for events scout",
    "when does event scout run?",
  ]) {
    const reply = await handler(eventFor(text), { channelId: "whatsapp", senderId: MEMBER });
    assert.equal(reply, undefined, `expected "${text}" to fall through to the LLM`);
  }
});

test("menu option 2 starts personalized news when no service state is active", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  const serviceStatePath = path.join(dir, "state.json");
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":true,"publicUserId":"usr_test"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ accountLinkScript, serviceStatePath });
  const reply = await handler(eventFor("2"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /What topics should I follow/);
});

test("webetu restaurant state makes option 2 wait instead of starting news", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  const serviceStatePath = path.join(dir, "state.json");
  writeServiceState(serviceStatePath, MEMBER, "webetu", "webetu.awaiting_restaurant");
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":true,"publicUserId":"usr_test"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ accountLinkScript, serviceStatePath });
  const reply = await handler(eventFor("2"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /Make manual reservations on Webetu/);
  assert.doesNotMatch(reply.text, /What topics should I follow/);
});

test("webetu restaurant state keeps option 1 inside the webetu branch", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  const serviceStatePath = path.join(dir, "state.json");
  writeServiceState(serviceStatePath, MEMBER, "webetu", "webetu.restaurant_not_found");
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":false}\\n' ;;
  create-invite) printf '{"setupUrl":"https://example.test/setup?token=abc"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ accountLinkScript, serviceStatePath });
  const reply = await handler(eventFor("1"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /exact restaurant name/);
  assert.doesNotMatch(reply.text, /https:\/\/example\.test\/setup/);
});

test("webetu restaurant state lets restaurant names continue to the agent", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const serviceStatePath = path.join(dir, "state.json");
  writeServiceState(serviceStatePath, MEMBER, "webetu", "webetu.awaiting_restaurant");

  const handler = registerHandler({ serviceStatePath });
  const reply = await handler(eventFor("Residence Test Restaurant"), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });

  assert.equal(reply, undefined);
});

test("webetu restaurant state lets Algerian residence shorthand continue to the agent", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const serviceStatePath = path.join(dir, "state.json");
  writeServiceState(serviceStatePath, MEMBER, "webetu", "webetu.awaiting_restaurant");

  const handler = registerHandler({ serviceStatePath });
  const reply = await handler(eventFor("RU 2000 Lits Tipaza"), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });

  assert.equal(reply, undefined);
});

test("news topic state treats the next message as topics", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  const newsSubscriptionsScript = path.join(dir, "news_subscriptions.py");
  const serviceStatePath = path.join(dir, "state.json");
  const newsLog = path.join(dir, "news.log");
  writeServiceState(serviceStatePath, MEMBER, "news", "news.awaiting_topics");
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":true,"publicUserId":"usr_test"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );
  fs.writeFileSync(
    newsSubscriptionsScript,
    `#!/usr/bin/env python3
import json
import sys
from pathlib import Path
Path(${JSON.stringify(newsLog)}).write_text(" ".join(sys.argv[1:]), encoding="utf-8")
print(json.dumps({"ok": True}))
`,
    { mode: 0o755 },
  );

  const handler = registerHandler({ accountLinkScript, newsSubscriptionsScript, serviceStatePath });
  const reply = await handler(eventFor("AI, football"), { channelId: "whatsapp", senderId: MEMBER });
  const newsArgs = fs.readFileSync(newsLog, "utf8");

  assert.equal(reply.handled, true);
  assert.match(reply.text, /registered for personalized news: AI, football/);
  assert.match(newsArgs, /register --phone \+233208864185 AI football/);
});

test("active service state can be cleared with menu", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const serviceStatePath = path.join(dir, "state.json");
  const greetingTemplatePath = path.join(dir, "public-greeting.txt");
  writeServiceState(serviceStatePath, MEMBER, "webetu", "webetu.awaiting_restaurant");
  fs.writeFileSync(greetingTemplatePath, "Choose a service");

  const handler = registerHandler({ greetingTemplatePath, serviceStatePath });
  const reply = await handler(eventFor("menu"), { channelId: "whatsapp", senderId: MEMBER });
  const saved = JSON.parse(fs.readFileSync(serviceStatePath, "utf8"));

  assert.equal(reply.handled, true);
  assert.equal(reply.text, "Choose a service");
  assert.deepEqual(saved.states, {});
});

test("expired service state is ignored and pruned", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  const serviceStatePath = path.join(dir, "state.json");
  writeServiceState(serviceStatePath, MEMBER, "webetu", "webetu.awaiting_restaurant", {
    expiresAt: new Date(Date.now() - 60_000).toISOString(),
  });
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":true,"publicUserId":"usr_test"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ accountLinkScript, serviceStatePath });
  const reply = await handler(eventFor("2"), { channelId: "whatsapp", senderId: MEMBER });
  const saved = JSON.parse(fs.readFileSync(serviceStatePath, "utf8"));

  assert.equal(reply.handled, true);
  assert.match(reply.text, /What topics should I follow/);
  assert.equal(saved.states[MEMBER]?.step, "news.awaiting_topics");
});

test("Done after a pending Webetu vault link validates setup and asks for restaurant", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const accountLinkScript = path.join(dir, "account-link");
  const pendingLinksPath = path.join(dir, "pending-links.json");
  const fakeBin = path.join(dir, "bin");
  const sudoLog = path.join(dir, "sudo.log");
  fs.mkdirSync(fakeBin);

  fs.writeFileSync(
    pendingLinksPath,
    JSON.stringify({
      version: 1,
      links: [
        {
          phone: MEMBER,
          service: "account-link",
          purpose: "webetu",
          nextPath: "/vault",
          setupUrl: "https://example.test/setup?token=abc",
          expiresAt: new Date(Date.now() + 60_000).toISOString(),
        },
      ],
    }),
  );
  writeExecutable(
    accountLinkScript,
    `#!/usr/bin/env bash
set -eu
case "$1" in
  status) printf '{"linked":true,"publicUserId":"usr_test"}\\n' ;;
  create-invite) printf '{"setupUrl":"https://example.test/setup?token=abc"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );
  writeExecutable(
    path.join(fakeBin, "sudo"),
    `#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "${sudoLog}"
cmd="$5"
case "$cmd" in
  lookup-user) printf '{"status":"ok","registration_status":"active"}\\n' ;;
  check-credentials) printf '{"status":"ok","has_username":true,"has_password":true}\\n' ;;
  reserve) printf '{"status":"dry_run","date":"2026-06-15","meals_reserved":0,"restaurant":"Test Restaurant"}\\n' ;;
  *) exit 2 ;;
esac
`,
  );

  const handler = registerHandler({ accountLinkScript, pendingLinksPath, workerSudoCommand: path.join(fakeBin, "sudo") });
  const reply = await handler(eventFor("Done"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /Which restaurant should I use by default/);
  const sudoCalls = fs.readFileSync(sudoLog, "utf8");
  assert.match(sudoCalls, /lookup-user/);
  assert.match(sudoCalls, /check-credentials/);
  assert.match(sudoCalls, /reserve/);
});

test("Done without Webetu setup context asks for clarification", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "public-service-gate-"));
  const handler = registerHandler({ pendingLinksPath: path.join(dir, "missing.json") });
  const reply = await handler(eventFor("Done"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /What did you finish setting up/);
});

test("inviteLinkText builds a one-tap wa.me invite link when bot number is configured", () => {
  const api = {
    pluginConfig: {
      botWhatsappNumber: "+263 785 632 943",
    },
  };

  const text = inviteLinkText(api, "4PG44766");

  assert.match(text, /https:\/\/wa\.me\/263785632943\?text=JOIN-4PG44766/);
  assert.match(text, /This invite works once\./);
});

test("inviteLinkText falls back to the join code when bot number is blank", () => {
  const api = {
    pluginConfig: {
      botWhatsappNumber: "",
    },
  };

  const text = inviteLinkText(api, "L59DGQ54");

  assert.match(text, /Invite a friend by asking them to message me:/);
  assert.match(text, /\n\nJOIN-L59DGQ54\n\n/);
  assert.doesNotMatch(text, /wa\.me/);
});

test("runJsonCommand parses JSON stdout", async () => {
  const result = await runJsonCommand("/bin/echo", ['{"status":"ok","count":2}']);

  assert.deepEqual(result, { status: "ok", count: 2 });
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
