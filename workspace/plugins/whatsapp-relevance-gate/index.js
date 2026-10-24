import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { classifyByEmbedding } from "./lib/embedding-classifier.js";
import {
  keywordMatch,
  normalizeText,
  shortContinuationMatch,
} from "./lib/keyword-classifier.js";

const PLUGIN_ID = "whatsapp-relevance-gate";
const RELEVANCE_MODE_ENV = "WHATSAPP_RELEVANCE_MODE";
const RELEVANCE_MODES = new Set(["keyword", "embedding", "off"]);
const PUBLIC_SERVICE_GATE_ID = "public-service-gate";

const DEFAULT_REJECTION =
  "I can help with making a CV, building materials, grafted fruit trees, " +
  "borehole drilling, solar and business marketing. What do you need?";
const DEFAULT_MAX_TEXT_LENGTH = 400;
const DEFAULT_LLM_MAX_TOKENS = 5;
const CACHE_TTL_MS = 10 * 60 * 1000;
const CACHE_MAX_ENTRIES = 500;

// normalized-text -> { verdict, expires }
const verdictCache = new Map();

function normalizePhone(value) {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  if (!trimmed) return "";
  const plus = trimmed.startsWith("+") ? "+" : "";
  return plus + trimmed.replace(/[^\d]/g, "");
}

function getEventChannel(event, ctx = {}) {
  return String(event?.channel ?? ctx?.channelId ?? ctx?.channel ?? "").trim();
}

function isWhatsAppChannel(value) {
  const channel = String(value ?? "").trim().toLowerCase();
  return channel === "whatsapp" || channel.startsWith("whatsapp:");
}

function textCandidate(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value
      .map((part) => {
        if (typeof part === "string") return part;
        if (typeof part?.text === "string") return part.text;
        if (typeof part?.content === "string") return part.content;
        return "";
      })
      .join(" ");
  }
  return "";
}

function getEventText(event = {}) {
  const candidates = [
    event.content,
    event.body,
    event.text,
    event.message?.text,
    event.message?.body,
    event.message?.content,
    event.payload?.text,
    event.payload?.body,
    event.payload?.content,
  ];
  for (const candidate of candidates) {
    const text = textCandidate(candidate).trim();
    if (text) return text;
  }
  return "";
}

function getEventSender(event = {}, ctx = {}) {
  return normalizePhone(
    event.senderId ??
      event.sender_id ??
      event.from ??
      event.sender?.e164 ??
      event.sender?.id ??
      event.sender?.phone ??
      event.message?.from ??
      event.payload?.from ??
      ctx.senderId ??
      ctx.sender_id ??
      ctx.from ??
      ctx.sender?.e164 ??
      ctx.sender?.id ??
      "",
  );
}

function getCurrentConfig(api) {
  return api.runtime?.config?.current?.() ?? api.config ?? {};
}

function getPluginConfig(api) {
  const live = getCurrentConfig(api).plugins?.entries?.[PLUGIN_ID]?.config;
  return live && typeof live === "object" ? live : api.pluginConfig ?? {};
}

function getPublicServiceGateConfig(api) {
  const live = getCurrentConfig(api).plugins?.entries?.[PUBLIC_SERVICE_GATE_ID]?.config;
  return live && typeof live === "object" ? live : {};
}

function isEnabled(api) {
  return getPluginConfig(api).enabled !== false;
}

// Three-way switch: "keyword" (default, unchanged behavior), "embedding" (the
// semantic classifier), or "off" (gate disabled — everything passes through).
// The WHATSAPP_RELEVANCE_MODE env var wins so it can be flipped without a config
// redeploy; the config `relevanceMode` key is the fallback default.
function getRelevanceMode(api) {
  const fromEnv = String(readEnvValue(RELEVANCE_MODE_ENV) ?? "").trim().toLowerCase();
  if (RELEVANCE_MODES.has(fromEnv)) return fromEnv;
  const fromCfg = String(getPluginConfig(api).relevanceMode ?? "").trim().toLowerCase();
  if (RELEVANCE_MODES.has(fromCfg)) return fromCfg;
  return "keyword";
}

function readOpenclawEnv() {
  try {
    const envPath = path.join(os.homedir(), ".openclaw", ".env");
    const text = fs.readFileSync(envPath, "utf8");
    const values = {};
    for (const rawLine of text.split("\n")) {
      let line = rawLine.trim();
      if (!line || line.startsWith("#")) continue;
      const eqIdx = line.indexOf("=");
      if (eqIdx === -1) continue;
      let key = line.slice(0, eqIdx).trim();
      if (key.startsWith("export ")) key = key.slice(7).trim();
      let value = line.slice(eqIdx + 1).trim();
      if (value.length >= 2 && value[0] === value[value.length - 1] && (value[0] === '"' || value[0] === "'")) {
        value = value.slice(1, -1);
      }
      if (key) values[key] = value;
    }
    return values;
  } catch {
    return {};
  }
}

function readEnvValue(name) {
  if (Object.prototype.hasOwnProperty.call(process.env, name)) return process.env[name];
  return readOpenclawEnv()[name];
}

function parseBoolean(value) {
  if (typeof value === "boolean") return value;
  if (typeof value !== "string") return undefined;
  const normalized = value.trim().toLowerCase();
  if (["1", "true", "yes", "y", "on"].includes(normalized)) return true;
  if (["0", "false", "no", "n", "off"].includes(normalized)) return false;
  return undefined;
}

function isAdminModeEnabled(api) {
  const envValue = parseBoolean(readEnvValue("WHATSAPP_ADMIN_ENABLED"));
  if (envValue !== undefined) return envValue;
  const publicServiceCfg = getPublicServiceGateConfig(api);
  const publicServiceValue = parseBoolean(publicServiceCfg.adminModeEnabled);
  if (publicServiceValue !== undefined) return publicServiceValue;
  const cfgValue = parseBoolean(getPluginConfig(api).adminModeEnabled);
  return cfgValue !== undefined ? cfgValue : true;
}

function getBypassUsers(api) {
  const cfg = getPluginConfig(api);
  const explicit = Array.isArray(cfg.bypassUsers) ? cfg.bypassUsers : [];
  const users = new Set(explicit.map(normalizePhone).filter(Boolean));
  if (!isAdminModeEnabled(api)) {
    const publicServiceCfg = getPublicServiceGateConfig(api);
    const admins = Array.isArray(publicServiceCfg.whatsappAdminUsers) ? publicServiceCfg.whatsappAdminUsers : [];
    for (const admin of admins.map(normalizePhone).filter(Boolean)) {
      users.delete(admin);
    }
  }
  return users;
}

function getRejectionMessage(api) {
  const cfg = getPluginConfig(api);
  return typeof cfg.rejectionMessage === "string" && cfg.rejectionMessage.trim()
    ? cfg.rejectionMessage.trim()
    : DEFAULT_REJECTION;
}

function failOpenVerdict(api) {
  // Default fail-open: a classifier outage must not silently block all traffic.
  return getPluginConfig(api).failOpen === false ? "irrelevant" : "relevant";
}

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

function parseVerdict(llmText) {
  const upper = String(llmText ?? "").toUpperCase();
  // Check IRRELEVANT first — it contains "RELEVANT" as a substring.
  if (upper.includes("IRRELEVANT")) return "irrelevant";
  if (upper.includes("RELEVANT")) return "relevant";
  return null;
}

function cacheGet(normalized) {
  const hit = verdictCache.get(normalized);
  if (!hit) return null;
  if (hit.expires <= Date.now()) {
    verdictCache.delete(normalized);
    return null;
  }
  return hit.verdict;
}

function cacheSet(normalized, verdict) {
  if (verdictCache.size >= CACHE_MAX_ENTRIES) {
    const oldest = verdictCache.keys().next().value;
    if (oldest !== undefined) verdictCache.delete(oldest);
  }
  verdictCache.set(normalized, { verdict, expires: Date.now() + CACHE_TTL_MS });
}

export async function classify(api, text, mode = getRelevanceMode(api)) {
  const cfg = getPluginConfig(api);
  const normalized = normalizeText(text);
  if (!normalized) return "relevant"; // nothing to judge → let agent handle

  const maxLen = Number.isInteger(cfg.maxTextLength) ? cfg.maxTextLength : DEFAULT_MAX_TEXT_LENGTH;
  if (normalized.length > maxLen) return failOpenVerdict(api);

  // Cache is keyed by mode so flipping the switch never returns the other
  // method's verdict for the same text.
  const cacheKey = `${mode}\n${normalized}`;
  const cached = cacheGet(cacheKey);
  if (cached) return cached;

  if (mode === "embedding") {
    // Keep the cheap, safe fast-path allows so trivial replies (menu digits,
    // continuations) never burn an embedding call.
    if (shortContinuationMatch(api, normalized)) {
      cacheSet(cacheKey, "relevant");
      return "relevant";
    }
    const verdict = await classifyByEmbedding(api, text);
    cacheSet(cacheKey, verdict);
    return verdict;
  }

  // --- keyword mode (default) — behavior unchanged ---

  // 1) Local keyword / example pass — free, fast, no LLM call.
  if (keywordMatch(api, normalized)) {
    cacheSet(cacheKey, "relevant");
    return "relevant";
  }

  // 2) LLM fallback for ambiguous messages.
  if (cfg.useLlmFallback === false) {
    // No semantic fallback configured → keyword miss means irrelevant.
    cacheSet(cacheKey, "irrelevant");
    return "irrelevant";
  }

  const complete = api.runtime?.llm?.complete;
  if (typeof complete !== "function") {
    api.logger?.warn?.(`${PLUGIN_ID}: api.runtime.llm.complete unavailable; failing ${failOpenVerdict(api)}`);
    return failOpenVerdict(api);
  }

  try {
    const summary =
      typeof cfg.serviceSummary === "string" && cfg.serviceSummary.trim()
        ? cfg.serviceSummary.trim()
        : "the services this assistant offers";
    const res = await complete({
      purpose: `${PLUGIN_ID}.classify`,
      maxTokens: Number.isInteger(cfg.llmMaxTokens) ? cfg.llmMaxTokens : DEFAULT_LLM_MAX_TOKENS,
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
    const verdict = parseVerdict(extractLlmText(res));
    if (!verdict) {
      api.logger?.warn?.(`${PLUGIN_ID}: unparseable classifier reply; failing ${failOpenVerdict(api)}`);
      return failOpenVerdict(api);
    }
    cacheSet(cacheKey, verdict);
    return verdict;
  } catch (err) {
    api.logger?.warn?.(`${PLUGIN_ID}: classifier error (${String(err)}); failing ${failOpenVerdict(api)}`);
    return failOpenVerdict(api);
  }
}

export default {
  id: PLUGIN_ID,
  name: "WhatsApp Relevance Gate",
  description: "Blocks inbound WhatsApp messages unrelated to the agent's services before agent dispatch.",
  version: "0.1.0",
  register(api) {
    api.on(
      "before_dispatch",
      async (event, ctx) => {
        if (!isEnabled(api)) return; // disabled → pass through

        const mode = getRelevanceMode(api);
        if (mode === "off") return; // kill switch → pass every message through

        const channel = getEventChannel(event, ctx);
        if (!isWhatsAppChannel(channel)) return; // only gate WhatsApp

        const sender = getEventSender(event, ctx);
        if (!sender) {
          api.logger?.warn?.(`${PLUGIN_ID}: WhatsApp before_dispatch missing sender; handled fail-closed`);
          return { handled: true, text: getRejectionMessage(api) };
        }
        if (sender && getBypassUsers(api).has(sender)) return; // operator/trusted → pass

        const text = getEventText(event);
        if (!text) return; // media-only / empty → let the agent handle it

        const verdict = await classify(api, text, mode);
        if (verdict === "relevant") return; // pass through to agent

        // Irrelevant → claim the message so the agent never runs, and reply.
        api.logger?.info?.(`${PLUGIN_ID}: rejected out-of-scope WhatsApp text sender=${sender} textLen=${text.length}`);
        return { handled: true, text: getRejectionMessage(api) };
      },
      { priority: -10 }, // run AFTER public-service-gate's specific handlers
    );
  },
};
