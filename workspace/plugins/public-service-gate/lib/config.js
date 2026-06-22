import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const RELEVANCE_GATE_ID = "whatsapp-relevance-gate";

export function readOpenclawEnv() {
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

export function resolveAppPublicBaseUrl() {
  if (process.env.PUBLIC_BASE_URL) return process.env.PUBLIC_BASE_URL.replace(/\/+$/, "");
  if (process.env.APP_BASE_URL) return process.env.APP_BASE_URL.replace(/\/+$/, "");
  const env = readOpenclawEnv();
  if (env.PUBLIC_BASE_URL) return env.PUBLIC_BASE_URL.replace(/\/+$/, "");
  if (env.APP_BASE_URL) return env.APP_BASE_URL.replace(/\/+$/, "");
  return "http://localhost:3000";
}

export function readEnvValue(name) {
  if (Object.prototype.hasOwnProperty.call(process.env, name)) return process.env[name];
  return readOpenclawEnv()[name];
}

export const RELEVANCE_MODE_ENV = "WHATSAPP_RELEVANCE_MODE";
const RELEVANCE_MODES = new Set(["keyword", "embedding", "off"]);

// Three-way relevance switch shared with the whatsapp-relevance-gate plugin:
// "keyword" (default), "embedding", or "off". Env wins over the config default.
export function getRelevanceMode(api) {
  const fromEnv = String(readEnvValue(RELEVANCE_MODE_ENV) ?? "").trim().toLowerCase();
  if (RELEVANCE_MODES.has(fromEnv)) return fromEnv;
  const cfg = getRelevanceGateConfig(api);
  const fromCfg = String(cfg.relevanceMode ?? "").trim().toLowerCase();
  if (RELEVANCE_MODES.has(fromCfg)) return fromCfg;
  return "keyword";
}

export function parseBoolean(value) {
  if (typeof value === "boolean") return value;
  if (typeof value !== "string") return undefined;
  const normalized = value.trim().toLowerCase();
  if (["1", "true", "yes", "y", "on"].includes(normalized)) return true;
  if (["0", "false", "no", "n", "off"].includes(normalized)) return false;
  return undefined;
}

export function expandHome(input) {
  if (typeof input !== "string" || input.length === 0) return input;
  if (input === "~") return os.homedir();
  if (input.startsWith("~/")) return path.join(os.homedir(), input.slice(2));
  return input;
}

export function getCurrentConfig(api) {
  return api.runtime?.config?.current?.() ?? api.config ?? {};
}

export function getPluginConfig(api) {
  const live = getCurrentConfig(api).plugins?.entries?.["public-service-gate"]?.config;
  return live && typeof live === "object" ? live : api.pluginConfig ?? {};
}

export function getRelevanceGateConfig(api) {
  const live = getCurrentConfig(api).plugins?.entries?.[RELEVANCE_GATE_ID]?.config;
  return live && typeof live === "object" ? live : {};
}
