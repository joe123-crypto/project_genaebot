// A/B harness: run the same labeled fixture through the keyword and embedding
// relevance classifiers and print a side-by-side report (accuracy, false
// blocks, false allows, latency, score spread). Use it to compare the two
// methods on real-world-style traffic and to tune relevanceFloor/relevanceMargin.
//
// Usage:
//   AI_GATEWAY_API_KEY=... node ab-harness.mjs
//   node ab-harness.mjs path/to/fixture.json   (optional custom fixture)
//
// The embedding column needs a reachable gateway (AI_GATEWAY_API_KEY); without
// it, embedding mode is permissive and every verdict is "relevant" (noted below).
//
// It also needs `openclaw` resolvable as an ESM bare import. In the deployed
// environment (~/.openclaw/workspace/plugins) that is automatic. To run from
// this repo, symlink it in first (ESM ignores NODE_PATH):
//   mkdir -p node_modules && \
//     ln -sfn "$(npm root -g)/openclaw" node_modules/openclaw
//   node ab-harness.mjs   # then: rm -rf node_modules

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { classify } from "./index.js";
import { scoreByEmbedding } from "./lib/embedding-classifier.js";

const here = path.dirname(fileURLToPath(import.meta.url));
// repo root: workspace/plugins/whatsapp-relevance-gate -> ../../../..
const repoRoot = path.resolve(here, "..", "..", "..");
const TEMPLATE = path.join(repoRoot, "config", "openclaw.template.json");

function loadRelevanceConfig() {
  const cfg = JSON.parse(fs.readFileSync(TEMPLATE, "utf8"));
  const relevance = cfg.plugins?.entries?.["whatsapp-relevance-gate"]?.config ?? {};
  const memorySearch = JSON.parse(JSON.stringify(cfg.memory?.search ?? {}));
  // The template stores apiKey as a dotenv SecretRef (resolved from ~/.openclaw/.env
  // in production). For local harness runs, substitute the literal env key so the
  // provider can authenticate without a populated dotenv file.
  if (process.env.AI_GATEWAY_API_KEY && memorySearch.remote) {
    memorySearch.remote = { ...memorySearch.remote, apiKey: process.env.AI_GATEWAY_API_KEY };
  }
  return { relevance, memorySearch };
}

// label: "relevant" = a real service request that SHOULD pass.
//        "irrelevant" = off-topic noise that SHOULD be blocked.
const DEFAULT_FIXTURE = [
  { text: "sign me up for daily news please", label: "relevant" },
  { text: "stop sending me the news", label: "relevant" },
  { text: "what jobs are available for students", label: "relevant" },
  { text: "any scholarships for engineering", label: "relevant" },
  { text: "help me apply for an internship", label: "relevant" },
  { text: "what events are happening this weekend near me", label: "relevant" },
  { text: "how do I get started with this", label: "relevant" },
  { text: "i need to set up my account", label: "relevant" },
  { text: "i am looking for work", label: "relevant" },
  // off-topic noise
  { text: "what's the weather like today", label: "irrelevant" },
  { text: "tell me a joke about cats", label: "irrelevant" },
  { text: "who won the champions league last night", label: "irrelevant" },
  { text: "what do you think of the president", label: "irrelevant" },
  { text: "what's the bitcoin price right now", label: "irrelevant" },
  { text: "write me a poem about the sea", label: "irrelevant" },
  { text: "translate this sentence to spanish", label: "irrelevant" },
  { text: "quelle heure est-il", label: "irrelevant" },
  { text: "raconte-moi une histoire", label: "irrelevant" },
  { text: "the restaurant food yesterday was terrible", label: "irrelevant", note: "near-service but not a request" },
  { text: "i hate my job so much", label: "irrelevant", note: "near-service but not a request" },
  { text: "do you know any good movies", label: "irrelevant" },
];

function buildApi(mode, relevance, memorySearch) {
  return {
    config: {
      memory: { search: memorySearch },
      plugins: {
        entries: {
          "whatsapp-relevance-gate": { config: { ...relevance, relevanceMode: mode } },
        },
      },
    },
    runtime: {},
    logger: { warn() {}, info() {} },
  };
}

async function run() {
  const fixturePath = process.argv[2];
  const fixture = fixturePath
    ? JSON.parse(fs.readFileSync(fixturePath, "utf8"))
    : DEFAULT_FIXTURE;

  const { relevance, memorySearch } = loadRelevanceConfig();
  const keywordApi = buildApi("keyword", relevance, memorySearch);
  const embeddingApi = buildApi("embedding", relevance, memorySearch);

  const hasKey = Boolean(process.env.AI_GATEWAY_API_KEY) || Boolean(memorySearch?.remote?.apiKey);
  if (!hasKey) {
    console.warn(
      "\n⚠  No AI_GATEWAY_API_KEY found — embedding mode will be permissive (all 'relevant').\n",
    );
  }

  const stats = {
    keyword: { ok: 0, falseBlock: 0, falseAllow: 0, ms: 0 },
    embedding: { ok: 0, falseBlock: 0, falseAllow: 0, ms: 0 },
  };
  const rows = [];

  for (const item of fixture) {
    const { text, label } = item;

    const kStart = performance.now();
    const kVerdict = await classify(keywordApi, text, "keyword");
    stats.keyword.ms += performance.now() - kStart;

    const eStart = performance.now();
    const eVerdict = await classify(embeddingApi, text, "embedding");
    stats.embedding.ms += performance.now() - eStart;

    let scores = null;
    try {
      scores = await scoreByEmbedding(embeddingApi, text);
    } catch {
      scores = null;
    }

    for (const [method, verdict] of [["keyword", kVerdict], ["embedding", eVerdict]]) {
      if (verdict === label) stats[method].ok += 1;
      else if (label === "relevant") stats[method].falseBlock += 1;
      else stats[method].falseAllow += 1;
    }

    rows.push({
      label,
      keyword: kVerdict,
      embedding: eVerdict,
      pos: scores ? scores.bestPositive.toFixed(3) : "—",
      neg: scores ? scores.bestNegative.toFixed(3) : "—",
      text: text.length > 44 ? text.slice(0, 41) + "…" : text,
      note: item.note ?? "",
    });
  }

  const pad = (s, n) => String(s).padEnd(n);
  console.log(
    "\n" +
      pad("LABEL", 11) +
      pad("KEYWORD", 11) +
      pad("EMBED", 11) +
      pad("POS", 7) +
      pad("NEG", 7) +
      "MESSAGE",
  );
  console.log("-".repeat(90));
  for (const r of rows) {
    const flag = (m) => (r[m] === r.label ? " " : "✗");
    console.log(
      pad(r.label, 11) +
        pad(`${r.keyword}${flag("keyword")}`, 11) +
        pad(`${r.embedding}${flag("embedding")}`, 11) +
        pad(r.pos, 7) +
        pad(r.neg, 7) +
        r.text +
        (r.note ? `  (${r.note})` : ""),
    );
  }

  const total = fixture.length;
  console.log("\n" + "=".repeat(60));
  for (const method of ["keyword", "embedding"]) {
    const s = stats[method];
    console.log(
      `${pad(method.toUpperCase(), 10)} accuracy ${((s.ok / total) * 100).toFixed(1)}%  ` +
        `false-block ${s.falseBlock}  false-allow ${s.falseAllow}  ` +
        `avg ${(s.ms / total).toFixed(1)}ms/msg`,
    );
  }
  console.log("=".repeat(60) + "\n");
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
