import assert from "node:assert/strict";
import test from "node:test";

import plugin from "./index.js";

const MEMBER = "+233208864185";
const ADMIN = "+213563719936";
const TRUSTED = "+15551234567";

function registerHandler(config = {}, complete, publicServiceGateConfig = {}) {
  let handler;
  const api = {
    config: {
      plugins: {
        entries: {
          "public-service-gate": {
            config: {
              whatsappAdminUsers: [],
              ...publicServiceGateConfig,
            },
          },
          "whatsapp-relevance-gate": {
            config: {
              enabled: true,
              serviceKeywords: ["webetu", "meal", "news", "job", "event"],
              serviceSummary: "Webetu meals, personalized news, local events, and job scouting",
              useLlmFallback: true,
              ...config,
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
      warn() {},
    },
    on(_name, callback) {
      handler = callback;
    },
  };
  plugin.register(api);
  return handler;
}

function eventFor(text, sender = MEMBER) {
  return {
    channel: "whatsapp",
    senderId: sender,
    content: text,
  };
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

test("short continuation replies pass without an LLM call", async () => {
  let calls = 0;
  const handler = registerHandler({}, async () => {
    calls += 1;
    return "IRRELEVANT";
  });

  const reply = await handler(eventFor("Done"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply, undefined);
  assert.equal(calls, 0);
});

test("menu digits pass without an LLM call", async () => {
  let calls = 0;
  const handler = registerHandler({}, async () => {
    calls += 1;
    return "IRRELEVANT";
  });

  const reply = await handler(eventFor("1"), { channelId: "whatsapp", senderId: MEMBER });

  assert.equal(reply, undefined);
  assert.equal(calls, 0);
});

test("restaurant names pass without an LLM call", async () => {
  let calls = 0;
  const handler = registerHandler({}, async () => {
    calls += 1;
    return "IRRELEVANT";
  });

  const reply = await handler(eventFor("الإقامة الجامعية 03 باب الزوار"), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });

  assert.equal(reply, undefined);
  assert.equal(calls, 0);
});

test("Algerian residence shorthand restaurant names pass without an LLM call", async () => {
  let calls = 0;
  const handler = registerHandler({}, async () => {
    calls += 1;
    return "IRRELEVANT";
  });

  for (const text of ["RU 2000 Lits Tipaza", "ru 2000 lits tipaza"]) {
    const reply = await handler(eventFor(text), {
      channelId: "whatsapp",
      senderId: MEMBER,
    });

    assert.equal(reply, undefined);
  }
  assert.equal(calls, 0);
});

test("bare residence shorthand does not pass as a restaurant name", async () => {
  let calls = 0;
  const handler = registerHandler({ useLlmFallback: false }, async () => {
    calls += 1;
    return "RELEVANT";
  });

  const reply = await handler(eventFor("ru"), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /Webetu meal reservations/);
  assert.equal(calls, 0);
});

test("explicit irrelevant classifier verdict still rejects", async () => {
  const handler = registerHandler({}, async () => "IRRELEVANT");
  const reply = await handler(eventFor("tell me the bitcoin price now"), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /Webetu meal reservations/);
  assert.doesNotMatch(reply.text, /Sorry, I can only help/);
});

test("configured bypass user passes through by default", async () => {
  await withAdminMode(undefined, async () => {
    let calls = 0;
    const handler = registerHandler({ bypassUsers: [ADMIN], useLlmFallback: false }, async () => {
      calls += 1;
      return "IRRELEVANT";
    });
    const reply = await handler(eventFor("tell me the bitcoin price now", ADMIN), {
      channelId: "whatsapp",
      senderId: ADMIN,
    });

    assert.equal(reply, undefined);
    assert.equal(calls, 0);
  });
});

test("configured admin bypass user is filtered when WhatsApp admin mode is disabled", async () => {
  await withAdminMode("false", async () => {
    let calls = 0;
    const handler = registerHandler(
      { bypassUsers: [ADMIN], useLlmFallback: false },
      async () => {
        calls += 1;
        return "IRRELEVANT";
      },
      { whatsappAdminUsers: [ADMIN] },
    );
    const reply = await handler(eventFor("tell me the bitcoin price now", ADMIN), {
      channelId: "whatsapp",
      senderId: ADMIN,
    });

    assert.equal(reply.handled, true);
    assert.match(reply.text, /Webetu meal reservations/);
    assert.equal(calls, 0);
  });
});

test("non-admin bypass users still pass when WhatsApp admin mode is disabled", async () => {
  await withAdminMode("false", async () => {
    let calls = 0;
    const handler = registerHandler(
      { bypassUsers: [ADMIN, TRUSTED], useLlmFallback: false },
      async () => {
        calls += 1;
        return "IRRELEVANT";
      },
      { whatsappAdminUsers: [ADMIN] },
    );
    const reply = await handler(eventFor("tell me the bitcoin price now", TRUSTED), {
      channelId: "whatsapp",
      senderId: TRUSTED,
    });

    assert.equal(reply, undefined);
    assert.equal(calls, 0);
  });
});

test("nested live-style event fields are still classified and rejected", async () => {
  const handler = registerHandler({ useLlmFallback: false });
  const reply = await handler(
    {
      channel: "whatsapp:default",
      sender: { e164: MEMBER },
      message: { text: "Tell me a joke about football" },
    },
    { channelId: "whatsapp:default" },
  );

  assert.equal(reply.handled, true);
  assert.match(reply.text, /Webetu meal reservations/);
});

test("WhatsApp message without a sender fails closed", async () => {
  const handler = registerHandler({ useLlmFallback: false });
  const reply = await handler({ channel: "whatsapp", content: "Tell me a joke" }, { channelId: "whatsapp" });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /Webetu meal reservations/);
});

test("classifier errors fail open by default", async () => {
  const handler = registerHandler({}, async () => {
    throw new Error("model unavailable");
  });

  const reply = await handler(eventFor("ambiguous unique classifier outage message"), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });

  assert.equal(reply, undefined);
});

test("classifier errors fail closed when configured", async () => {
  const handler = registerHandler({ failOpen: false }, async () => {
    throw new Error("model unavailable");
  });

  const reply = await handler(eventFor("ambiguous unique classifier outage message"), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });

  assert.equal(reply.handled, true);
  assert.match(reply.text, /Webetu meal reservations/);
});
