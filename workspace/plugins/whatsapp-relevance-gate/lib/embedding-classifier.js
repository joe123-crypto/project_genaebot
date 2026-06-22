// Embedding-based relevance classifier — an alternative to the keyword gate,
// selected by the WHATSAPP_RELEVANCE_MODE switch (see index.js). It embeds the
// incoming message with a multilingual model and compares it (cosine) against
// curated positive (service) and negative (off-topic) anchor phrases. A message
// is relevant when it is closest to a service anchor by a configurable margin.
//
// Design notes:
//   * Pure: no dependency back on index.js. Callers add their own cheap
//     fast-path allows (menu digits, restaurant names) before delegating here.
//   * Permissive: any failure (no key, provider/SDK unavailable, API error,
//     empty anchors) returns the fail-open verdict so an outage never blocks
//     real users. Set failOpen:false in config to invert.
//   * The model is pinned to google/text-multilingual-embedding-002 via the
//     Vercel AI gateway (reuses agents.defaults.memorySearch.remote), and is
//     overridable through the `embeddingModel` config key.

const PLUGIN_ID = "whatsapp-relevance-gate";

const DEFAULT_EMBEDDING_MODEL = "google/text-multilingual-embedding-002";
const DEFAULT_RELEVANCE_FLOOR = 0.55;
const DEFAULT_RELEVANCE_MARGIN = 0.05;
const EMBED_INPUT_TYPE = "classification";

// Sensible multilingual (EN/FR/AR) defaults so embedding mode works even before
// the config anchors are tuned. Config `serviceAnchors` / `offTopicAnchors`
// override these entirely when present.
const DEFAULT_SERVICE_ANCHORS = [
  // meal reservation / Webetu
  "reserve a meal at the university restaurant",
  "book my Webetu meal for tomorrow",
  "I want to reserve lunch at the resto u",
  "réserver un repas au restaurant universitaire",
  "احجز وجبة في المطعم الجامعي",
  // personalized news
  "subscribe me to personalized news",
  "start sending me daily news updates",
  "stop the news updates",
  "abonne-moi aux actualités",
  // local events
  "what events are happening near me",
  "find local events this weekend",
  "y a-t-il des événements près de chez moi",
  // jobs / scholarships / internships
  "find me a job",
  "any scholarships I can apply to",
  "help me apply for an internship",
  "cherche-moi un emploi ou une bourse",
  "ابحث لي عن وظيفة او منحة",
  // invites / onboarding
  "send me an invite link for my friend",
  "how do I get started",
  "help me set up my account",
];
const DEFAULT_OFFTOPIC_ANCHORS = [
  "what is the weather today",
  "tell me a joke",
  "who won the football match last night",
  "what do you think about politics",
  "what is the price of bitcoin",
  "can you write me a poem",
  "translate this paragraph for me",
  "I am bored, talk to me",
  "quelle est la météo aujourd'hui",
  "raconte-moi une blague",
  "ما هي أخبار كرة القدم",
];

// --- config ----------------------------------------------------------------

function getCurrentConfig(api) {
  return api.runtime?.config?.current?.() ?? api.config ?? {};
}

function getRelevanceConfig(api) {
  const live = getCurrentConfig(api).plugins?.entries?.[PLUGIN_ID]?.config;
  if (live && typeof live === "object") return live;
  return api.pluginConfig && typeof api.pluginConfig === "object" ? api.pluginConfig : {};
}

function failOpenVerdict(api) {
  // Embedding mode is permissive by default (the chosen bias): an outage or
  // missing provider lets traffic through. This is independent of the keyword
  // path's `failOpen` so flipping it never changes keyword behavior. Set
  // `embeddingFailOpen: false` to make embedding mode block on failure instead.
  return getRelevanceConfig(api).embeddingFailOpen === false ? "irrelevant" : "relevant";
}

function anchorList(value, fallback) {
  const list = Array.isArray(value) ? value.map((x) => String(x ?? "").trim()).filter(Boolean) : [];
  return list.length > 0 ? list : fallback;
}

// --- cosine (with inline fallback if the SDK util is unavailable) -----------

function inlineCosine(a, b) {
  if (!a?.length || !b?.length) return 0;
  const len = Math.min(a.length, b.length);
  let dot = 0;
  let normA = 0;
  let normB = 0;
  for (let i = 0; i < len; i += 1) {
    const av = a[i] ?? 0;
    const bv = b[i] ?? 0;
    dot += av * bv;
    normA += av * av;
    normB += bv * bv;
  }
  if (normA === 0 || normB === 0) return 0;
  return dot / (Math.sqrt(normA) * Math.sqrt(normB));
}

// --- embedding provider (lazy, cached) -------------------------------------

let sdkPromise;
async function loadSdk() {
  if (!sdkPromise) {
    sdkPromise = (async () => {
      const [providers, storage] = await Promise.all([
        import("openclaw/plugin-sdk/embedding-providers"),
        import("openclaw/plugin-sdk/memory-core-host-engine-storage").catch(() => null),
      ]);
      return {
        getEmbeddingProvider: providers.getEmbeddingProvider,
        cosineSimilarity:
          storage && typeof storage.cosineSimilarity === "function"
            ? storage.cosineSimilarity
            : inlineCosine,
      };
    })().catch((err) => {
      sdkPromise = undefined; // allow a later retry
      throw err;
    });
  }
  return sdkPromise;
}

// model -> { embedBatch(texts) -> number[][], cosine }
const providerCache = new Map();

function getEmbeddingModel(api) {
  const cfg = getRelevanceConfig(api);
  return typeof cfg.embeddingModel === "string" && cfg.embeddingModel.trim()
    ? cfg.embeddingModel.trim()
    : DEFAULT_EMBEDDING_MODEL;
}

// Resolve an embedder: either a test/runtime-injected hook, or one built from
// the published embedding-provider SDK using the gateway remote the project
// already configures for memory search. Returns { embedBatch, cosine } or null.
async function getEmbedder(api) {
  // Injectable hook (used by tests; mirrors api.runtime.llm.complete).
  const injected = api.runtime?.embeddings;
  if (injected && typeof injected.embedBatch === "function") {
    return { embedBatch: (texts) => injected.embedBatch(texts), cosine: injected.cosine ?? inlineCosine };
  }
  if (injected && typeof injected.embed === "function") {
    return {
      embedBatch: (texts) => Promise.all(texts.map((t) => injected.embed(t))),
      cosine: injected.cosine ?? inlineCosine,
    };
  }

  const model = getEmbeddingModel(api);
  if (providerCache.has(model)) return providerCache.get(model);

  const cfg = getCurrentConfig(api);
  const remote = cfg.agents?.defaults?.memorySearch?.remote;
  if (!remote || !remote.baseUrl) return null; // no gateway configured

  const { getEmbeddingProvider, cosineSimilarity } = await loadSdk();
  const adapter = getEmbeddingProvider("openai-compatible", cfg);
  if (!adapter) return null;
  const { provider } = await adapter.create({
    config: cfg,
    provider: "openai-compatible",
    model,
    remote,
  });
  if (!provider || typeof provider.embedBatch !== "function") return null;

  const embedder = {
    embedBatch: (texts) => provider.embedBatch(texts, { inputType: EMBED_INPUT_TYPE }),
    cosine: cosineSimilarity ?? inlineCosine,
  };
  providerCache.set(model, embedder);
  return embedder;
}

// --- anchor embeddings (lazy, cached by content + model) -------------------

// key `${model}\n${joinedAnchors}` -> { positive: number[][], negative: number[][] }
const anchorCache = new Map();

async function getAnchorVectors(api, embedder) {
  const cfg = getRelevanceConfig(api);
  const positive = anchorList(cfg.serviceAnchors, DEFAULT_SERVICE_ANCHORS);
  const negative = anchorList(cfg.offTopicAnchors, DEFAULT_OFFTOPIC_ANCHORS);
  const model = getEmbeddingModel(api);
  const key = `${model}\n+${positive.join("|")}\n-${negative.join("|")}`;
  const cached = anchorCache.get(key);
  if (cached) return cached;

  const all = await embedder.embedBatch([...positive, ...negative]);
  const vectors = {
    positive: all.slice(0, positive.length),
    negative: all.slice(positive.length),
  };
  anchorCache.set(key, vectors);
  return vectors;
}

// --- classify --------------------------------------------------------------

function bestSimilarity(cosine, target, anchors) {
  let best = 0;
  for (const vec of anchors) {
    const sim = cosine(target, vec);
    if (sim > best) best = sim;
  }
  return best;
}

// classifyByEmbedding(api, text) -> "relevant" | "irrelevant"
// Pure embedding decision: callers apply their own cheap fast-path allows first.
export async function classifyByEmbedding(api, text) {
  const cfg = getRelevanceConfig(api);
  const message = String(text ?? "").trim();
  if (!message) return "relevant"; // nothing to judge → let the agent handle it

  let embedder;
  try {
    embedder = await getEmbedder(api);
  } catch (err) {
    api.logger?.warn?.(`${PLUGIN_ID}: embedding provider unavailable (${String(err)}); failing ${failOpenVerdict(api)}`);
    return failOpenVerdict(api);
  }
  if (!embedder) {
    api.logger?.warn?.(`${PLUGIN_ID}: no embedding provider configured; failing ${failOpenVerdict(api)}`);
    return failOpenVerdict(api);
  }

  try {
    const anchors = await getAnchorVectors(api, embedder);
    if (anchors.positive.length === 0) return failOpenVerdict(api); // nothing to match against

    const [vec] = await embedder.embedBatch([message]);
    if (!Array.isArray(vec) || vec.length === 0) return failOpenVerdict(api);

    const floor = Number.isFinite(cfg.relevanceFloor) ? cfg.relevanceFloor : DEFAULT_RELEVANCE_FLOOR;
    const margin = Number.isFinite(cfg.relevanceMargin) ? cfg.relevanceMargin : DEFAULT_RELEVANCE_MARGIN;
    const bestPos = bestSimilarity(embedder.cosine, vec, anchors.positive);
    const bestNeg = bestSimilarity(embedder.cosine, vec, anchors.negative);

    if (bestPos >= floor && bestPos >= bestNeg + margin) return "relevant";
    return "irrelevant";
  } catch (err) {
    api.logger?.warn?.(`${PLUGIN_ID}: embedding classify error (${String(err)}); failing ${failOpenVerdict(api)}`);
    return failOpenVerdict(api);
  }
}

// Exposed for the A/B harness so it can report the raw scores behind a verdict.
export async function scoreByEmbedding(api, text) {
  const embedder = await getEmbedder(api);
  if (!embedder) return null;
  const anchors = await getAnchorVectors(api, embedder);
  const [vec] = await embedder.embedBatch([String(text ?? "").trim()]);
  return {
    bestPositive: bestSimilarity(embedder.cosine, vec, anchors.positive),
    bestNegative: bestSimilarity(embedder.cosine, vec, anchors.negative),
  };
}

// Test/maintenance helper: clear the in-module caches.
export function __resetEmbeddingCaches() {
  providerCache.clear();
  anchorCache.clear();
  sdkPromise = undefined;
}
