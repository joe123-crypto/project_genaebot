import fs from "node:fs";
import path from "node:path";

import { expandHome, getPluginConfig } from "./config.js";
import { normalizePhone, parseIsoTime } from "./events.js";

const DEFAULT_SERVICE_STATE_PATH = "~/.openclaw/runtime/public-service-gate/state.json";
const SERVICE_STATE_TTL_MS = 30 * 60 * 1000;

export function getServiceStatePath(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.serviceStatePath === "string" && pluginCfg.serviceStatePath.trim()
      ? pluginCfg.serviceStatePath.trim()
      : DEFAULT_SERVICE_STATE_PATH;
  return expandHome(configuredPath);
}

export function readServiceStates(api) {
  try {
    const raw = fs.readFileSync(getServiceStatePath(api), "utf8");
    const parsed = JSON.parse(raw);
    const states = parsed?.states && typeof parsed.states === "object" ? parsed.states : parsed;
    return states && typeof states === "object" && !Array.isArray(states) ? states : {};
  } catch (err) {
    api.logger?.debug?.(`public-service-gate: service state cache unavailable (${String(err)})`);
    return {};
  }
}

export function writeServiceStates(api, states) {
  const statePath = getServiceStatePath(api);
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  const tmpPath = path.join(path.dirname(statePath), `.${path.basename(statePath)}.${process.pid}.tmp`);
  fs.writeFileSync(tmpPath, JSON.stringify({ version: 1, states }, null, 2) + "\n", { encoding: "utf8", mode: 0o600 });
  fs.renameSync(tmpPath, statePath);
}

export function pruneServiceStates(states, now = Date.now()) {
  const next = {};
  let changed = false;
  for (const [phone, state] of Object.entries(states || {})) {
    if (!state || typeof state !== "object") {
      changed = true;
      continue;
    }
    const expiresAt = parseIsoTime(state.expiresAt);
    if (!expiresAt || expiresAt.getTime() <= now) {
      changed = true;
      continue;
    }
    next[phone] = state;
  }
  return { states: next, changed };
}

export function loadServiceState(api, phone) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return null;
  const loaded = readServiceStates(api);
  const pruned = pruneServiceStates(loaded);
  if (pruned.changed) {
    try {
      writeServiceStates(api, pruned.states);
    } catch (err) {
      api.logger?.warn?.(`public-service-gate: failed to prune service state (${String(err)})`);
    }
  }
  const state = pruned.states[normalizedPhone];
  return state && typeof state === "object" ? state : null;
}

export function saveServiceState(api, phone, service, step, data = {}) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return;
  const loaded = readServiceStates(api);
  const pruned = pruneServiceStates(loaded).states;
  pruned[normalizedPhone] = {
    phone: normalizedPhone,
    service,
    step,
    data: data && typeof data === "object" ? data : {},
    updatedAt: new Date().toISOString(),
    expiresAt: new Date(Date.now() + SERVICE_STATE_TTL_MS).toISOString(),
  };
  writeServiceStates(api, pruned);
}

export function clearServiceState(api, phone) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return;
  const loaded = readServiceStates(api);
  const pruned = pruneServiceStates(loaded).states;
  if (!Object.prototype.hasOwnProperty.call(pruned, normalizedPhone)) return;
  delete pruned[normalizedPhone];
  writeServiceStates(api, pruned);
}
