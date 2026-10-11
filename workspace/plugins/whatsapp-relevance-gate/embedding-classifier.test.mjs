import assert from "node:assert/strict";
import test from "node:test";

import plugin from "./index.js";
import { classifyByEmbedding, __resetEmbeddingCaches } from "./lib/embedding-classifier.js";

const MEMBER = "+233208864185";

// Vectors chosen so cosine separates them cleanly: SERVICE≈[1,0], OFFTOPIC≈[0,1].
const VECTORS = {
  SERVICE: [1, 0],
  OFFTOPIC: [0, 1],
  "i want lunch please": [1, 0],
  "whats the weather like": [0, 1],
  "halfway ambiguous text": [1, 1],
};

function fakeEmbeddings(extra = {}) {
  const map = { ...VECTORS, ...extra };
  let calls = 0;
  return {
    get calls() {
      return calls;
    },
    embedBatch: async (texts) => {
      calls += 1;
      return texts.map((t) => map[t] ?? [0, 0]);
    },
  };
}

function embApi({ config = {}, embeddings, withGateway = false } = {}) {
  return {
    config: {
      memory: withGateway
        ? { search: { remote: { baseUrl: "https://gateway.example/v1" } } }
        : {},
      plugins: {
        entries: {
          "whatsapp-relevance-gate": {
            config: {
              serviceAnchors: ["SERVICE"],
              offTopicAnchors: ["OFFTOPIC"],
              relevanceFloor: 0.55,
              relevanceMargin: 0.05,
              ...config,
            },
          },
        },
      },
    },
    runtime: embeddings ? { embeddings } : {},
    logger: { warn() {} },
  };
}

test.beforeEach(() => __resetEmbeddingCaches());

test("embedding: a service-like message is relevant", async () => {
  const api = embApi({ embeddings: fakeEmbeddings() });
  assert.equal(await classifyByEmbedding(api, "i want lunch please"), "relevant");
});

test("embedding: an off-topic message is irrelevant", async () => {
  const api = embApi({ embeddings: fakeEmbeddings() });
  assert.equal(await classifyByEmbedding(api, "whats the weather like"), "irrelevant");
});

test("embedding: a message near both anchors fails the margin and is irrelevant", async () => {
  const api = embApi({ embeddings: fakeEmbeddings() });
  assert.equal(await classifyByEmbedding(api, "halfway ambiguous text"), "irrelevant");
});

test("embedding: permissive (relevant) when no provider is available", async () => {
  const api = embApi({}); // no injected embeddings, no gateway remote
  assert.equal(await classifyByEmbedding(api, "i want lunch please"), "relevant");
});

test("embedding: fail-closed (irrelevant) on unavailable provider when embeddingFailOpen:false", async () => {
  const api = embApi({ config: { embeddingFailOpen: false } });
  assert.equal(await classifyByEmbedding(api, "i want lunch please"), "irrelevant");
});

test("embedding: permissive when embedBatch throws", async () => {
  const api = embApi({
    embeddings: {
      embedBatch: async () => {
        throw new Error("gateway down");
      },
    },
  });
  assert.equal(await classifyByEmbedding(api, "i want lunch please"), "relevant");
});

test("embedding: empty message is relevant without embedding", async () => {
  const embeddings = fakeEmbeddings();
  const api = embApi({ embeddings });
  assert.equal(await classifyByEmbedding(api, "   "), "relevant");
  assert.equal(embeddings.calls, 0);
});

// --- integration through the before_dispatch handler ---

function registerEmbeddingHandler(config = {}, embeddings) {
  let handler;
  const api = {
    config: {
      plugins: {
        entries: {
          "public-service-gate": { config: { whatsappAdminUsers: [] } },
          "whatsapp-relevance-gate": {
            config: {
              enabled: true,
              relevanceMode: "embedding",
              serviceAnchors: ["SERVICE"],
              offTopicAnchors: ["OFFTOPIC"],
              relevanceFloor: 0.55,
              relevanceMargin: 0.05,
              ...config,
            },
          },
        },
      },
    },
    runtime: embeddings ? { embeddings } : {},
    logger: { warn() {}, info() {} },
    on(_name, callback) {
      handler = callback;
    },
  };
  plugin.register(api);
  return handler;
}

function eventFor(text, sender = MEMBER) {
  return { channel: "whatsapp", senderId: sender, content: text };
}

test("embedding mode (config): off-topic message is rejected through the handler", async () => {
  const handler = registerEmbeddingHandler({}, fakeEmbeddings());
  const reply = await handler(eventFor("whats the weather like"), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });
  assert.equal(reply.handled, true);
  assert.match(reply.text, /personalized news/);
});

test("embedding mode (config): service message passes through the handler", async () => {
  const handler = registerEmbeddingHandler({}, fakeEmbeddings());
  const reply = await handler(eventFor("i want lunch please"), {
    channelId: "whatsapp",
    senderId: MEMBER,
  });
  assert.equal(reply, undefined);
});

test("embedding mode: continuation fast-path passes without an embedding call", async () => {
  const embeddings = fakeEmbeddings();
  const handler = registerEmbeddingHandler({}, embeddings);
  const reply = await handler(eventFor("done"), { channelId: "whatsapp", senderId: MEMBER });
  assert.equal(reply, undefined);
  assert.equal(embeddings.calls, 0);
});

test("off mode (env): every message passes through, even off-topic", async () => {
  const prev = process.env.WHATSAPP_RELEVANCE_MODE;
  process.env.WHATSAPP_RELEVANCE_MODE = "off";
  try {
    const embeddings = fakeEmbeddings();
    const handler = registerEmbeddingHandler({}, embeddings);
    const reply = await handler(eventFor("whats the weather like"), {
      channelId: "whatsapp",
      senderId: MEMBER,
    });
    assert.equal(reply, undefined);
    assert.equal(embeddings.calls, 0);
  } finally {
    if (prev === undefined) delete process.env.WHATSAPP_RELEVANCE_MODE;
    else process.env.WHATSAPP_RELEVANCE_MODE = prev;
  }
});

test("env mode wins over config mode", async () => {
  const prev = process.env.WHATSAPP_RELEVANCE_MODE;
  process.env.WHATSAPP_RELEVANCE_MODE = "off";
  try {
    // config says embedding, env says off → off wins, message passes through.
    const handler = registerEmbeddingHandler({ relevanceMode: "embedding" }, fakeEmbeddings());
    const reply = await handler(eventFor("whats the weather like"), {
      channelId: "whatsapp",
      senderId: MEMBER,
    });
    assert.equal(reply, undefined);
  } finally {
    if (prev === undefined) delete process.env.WHATSAPP_RELEVANCE_MODE;
    else process.env.WHATSAPP_RELEVANCE_MODE = prev;
  }
});
