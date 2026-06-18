import assert from "node:assert/strict";
import test from "node:test";

import plugin from "./index.js";

const MEMBER = "+233208864185";

function registerHandler(config = {}, complete) {
  let handler;
  const api = {
    config: {
      plugins: {
        entries: {
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

function eventFor(text) {
  return {
    channel: "whatsapp",
    senderId: MEMBER,
    content: text,
  };
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
