// Keyword-based relevance classifier — the default gate mode (see index.js),
// the fast, free counterpart to lib/embedding-classifier.js. It normalizes the
// message and matches it against curated keyword/example/allow-phrase lists,
// all read from plugin config.
//
// Design notes (same contract as embedding-classifier.js):
//   * Pure: no dependency back on index.js. It reads plugin config off `api`
//     directly, so index.js and this module stay decoupled.
//   * The neutralized-keyword guard keeps generic/number/JOIN entries in the
//     curated list without letting them wave through unrelated traffic.

const PLUGIN_ID = "whatsapp-relevance-gate";

const DEFAULT_SHORT_CONTINUATION_KEYWORDS = [
  "1",
  "2",
  "3",
  "4",
  "done",
  "finished",
  "complete",
  "completed",
  "saved",
  "ok",
  "okay",
  "yes",
  "no",
  "sure",
  "thanks",
  "thank you",
];
// Generic acknowledgements / fillers that appear in the curated allow-list but
// must never allow a message as a substring (e.g. "ok so what's the weather"
// contains "ok"). They are kept in the list but only match an exact whole
// message, where the existing continuation handling already lets them pass.
const GENERIC_STOPWORDS = new Set([
  "ok",
  "okay",
  "k",
  "yes",
  "yeah",
  "yep",
  "no",
  "nope",
  "hi",
  "hie",
  "hey",
  "hello",
  "now",
  "done",
  "finished",
  "complete",
  "completed",
  "saved",
  "run",
  "run it",
  "thanks",
  "thank you",
  "thanx",
  "thx",
  "thansks",
  "merci",
  "sure",
  "please",
  "wait",
  "exactly",
  "oi",
  "great",
  "ok great",
  "do a manual run",
]);

function getCurrentConfig(api) {
  return api.runtime?.config?.current?.() ?? api.config ?? {};
}

function getPluginConfig(api) {
  const live = getCurrentConfig(api).plugins?.entries?.[PLUGIN_ID]?.config;
  return live && typeof live === "object" ? live : api.pluginConfig ?? {};
}

export function normalizeText(text) {
  return String(text ?? "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

function getShortContinuationKeywords(api) {
  const cfg = getPluginConfig(api);
  const configured = Array.isArray(cfg.shortContinuationKeywords)
    ? cfg.shortContinuationKeywords.map(normalizeText).filter(Boolean)
    : [];
  return configured.length > 0 ? configured : DEFAULT_SHORT_CONTINUATION_KEYWORDS;
}

export function shortContinuationMatch(api, normalized) {
  return getShortContinuationKeywords(api).includes(normalized);
}

// A curated keyword is "neutralized" — only matched against an exact whole
// message, never as a substring — when it is a generic filler, a bare number,
// or a JOIN code. This keeps such entries in the allow-list (so the list stays
// the source of truth) without letting them wave through unrelated traffic that
// merely contains them.
function isNeutralizedKeyword(k) {
  if (!k) return true;
  if (GENERIC_STOPWORDS.has(k)) return true;
  if (/^\d+$/.test(k)) return true;
  if (/^join[-\s]?[a-z0-9]{4,}$/i.test(k)) return true;
  return false;
}

// Match a normalized message against a flat keyword list, applying the
// neutralized-keyword guard so generic/number/JOIN entries only match exactly.
function matchKeywordList(list, normalized) {
  if (!Array.isArray(list)) return false;
  for (const kw of list) {
    const k = normalizeText(kw);
    if (!k) continue;
    if (isNeutralizedKeyword(k)) {
      if (normalized === k) return true; // exact whole-message match only
      continue;
    }
    if (normalized.includes(k)) return true;
  }
  return false;
}

export function keywordMatch(api, normalized) {
  const cfg = getPluginConfig(api);
  if (shortContinuationMatch(api, normalized)) return true;
  // serviceKeywords: hand-maintained list. curatedAllowPhrases: real user
  // phrases sourced from keywords_cleaned.txt. Both honour the same guard.
  if (matchKeywordList(cfg.serviceKeywords, normalized)) return true;
  if (matchKeywordList(cfg.curatedAllowPhrases, normalized)) return true;
  const examples = Array.isArray(cfg.serviceExamples) ? cfg.serviceExamples : [];
  for (const ex of examples) {
    const e = normalizeText(ex);
    if (!e) continue;
    // Match either direction so short user messages still hit longer examples.
    if (normalized.includes(e) || e.includes(normalized)) return true;
  }
  return false;
}
