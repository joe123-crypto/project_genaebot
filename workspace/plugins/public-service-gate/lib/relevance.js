import { getRelevanceGateConfig } from "./config.js";
import { normalizeText } from "./events.js";

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
const DEFAULT_RELEVANCE_MAX_TEXT_LENGTH = 400;
const DEFAULT_RELEVANCE_LLM_MAX_TOKENS = 5;

function extractLlmText(res) {
  if (!res) return "";
  if (typeof res === "string") return res;
  if (typeof res.text === "string") return res.text;
  const content = res.content ?? res.message?.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) =>
        typeof part === "string" ? part : typeof part?.text === "string" ? part.text : "",
      )
      .join(" ");
  }
  return "";
}

function parseRelevanceVerdict(llmText) {
  const upper = String(llmText ?? "").toUpperCase();
  if (upper.includes("IRRELEVANT")) return "irrelevant";
  if (upper.includes("RELEVANT")) return "relevant";
  return null;
}

export function getShortContinuationKeywords(api) {
  const cfg = getRelevanceGateConfig(api);
  const configured = Array.isArray(cfg.shortContinuationKeywords)
    ? cfg.shortContinuationKeywords.map(normalizeText).filter(Boolean)
    : [];
  return configured.length > 0 ? configured : DEFAULT_SHORT_CONTINUATION_KEYWORDS;
}

function relevanceKeywordMatch(api, normalized) {
  if (getShortContinuationKeywords(api).includes(normalized)) return true;
  const cfg = getRelevanceGateConfig(api);
  const keywords = Array.isArray(cfg.serviceKeywords) ? cfg.serviceKeywords : [];
  for (const kw of keywords) {
    const k = normalizeText(kw);
    if (k && normalized.includes(k)) return true;
  }
  const examples = Array.isArray(cfg.serviceExamples) ? cfg.serviceExamples : [];
  for (const ex of examples) {
    const e = normalizeText(ex);
    if (!e) continue;
    if (normalized.includes(e) || e.includes(normalized)) return true;
  }
  return false;
}

// The set of "known service request" detectors lives in the per-service modules,
// so the caller (index.js) injects a single `isKnownServiceRequest(text)` predicate
// composed from them. This keeps lib/ free of any dependency on services/.
export async function isUnknownSenderRelevant(api, text, { isKnownServiceRequest } = {}) {
  if (typeof isKnownServiceRequest === "function" && isKnownServiceRequest(text)) return true;

  const normalized = normalizeText(text);
  if (!normalized) return false;

  const cfg = getRelevanceGateConfig(api);
  if (cfg.enabled === false) return false;

  const maxLen = Number.isInteger(cfg.maxTextLength) ? cfg.maxTextLength : DEFAULT_RELEVANCE_MAX_TEXT_LENGTH;
  if (normalized.length > maxLen) return false;

  if (relevanceKeywordMatch(api, normalized)) return true;
  if (cfg.useLlmFallback === false) return false;

  const complete = api.runtime?.llm?.complete;
  if (typeof complete !== "function") return false;

  try {
    const summary =
      typeof cfg.serviceSummary === "string" && cfg.serviceSummary.trim()
        ? cfg.serviceSummary.trim()
        : "the services this assistant offers";
    const res = await complete({
      purpose: "public-service-gate.unknown_sender_relevance",
      maxTokens: Number.isInteger(cfg.llmMaxTokens) ? cfg.llmMaxTokens : DEFAULT_RELEVANCE_LLM_MAX_TOKENS,
      temperature: 0,
      messages: [
        {
          role: "user",
          content:
            `Services offered: ${summary}\n\n` +
            `Is the following user message a request related to these services? ` +
            `Answer with exactly one word: RELEVANT or IRRELEVANT.\n\n` +
            `Message: ${text}`,
        },
      ],
    });
    return parseRelevanceVerdict(extractLlmText(res)) === "relevant";
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: unknown sender classifier error (${String(err)})`);
    return false;
  }
}
