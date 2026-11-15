import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { expandHome, getPluginConfig } from "./config.js";

// Shared disk contract with deploy/railway/console_customer_bot.py. The API
// replaces the whole file; prompt construction never sees a partial publish.
const TEXT_LIMIT = 16 * 1024;
const SNAPSHOT_LIMIT = 256 * 1024;
const unavailable = "Business info is unavailable right now. Do not quote any price or detail from chat history; use the handoff action instead.";
const defaultInstructions = new URL("../../../docs/public-whatsapp-reply-instructions.txt", import.meta.url);
const decoder = new TextDecoder("utf-8", { fatal: true });

function stateDirectory() {
  return process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), ".openclaw");
}

function readBounded(file, limit) {
  const fd = fs.openSync(file, "r");
  try {
    const buffer = Buffer.alloc(limit + 1);
    let length = 0;
    while (length < buffer.length) {
      const count = fs.readSync(fd, buffer, length, buffer.length - length);
      if (!count) break;
      length += count;
    }
    if (length > limit) throw new Error("Customer bot text exceeds its size limit");
    return decoder.decode(buffer.subarray(0, length));
  } finally {
    fs.closeSync(fd);
  }
}

function clean(value) {
  if (typeof value !== "string" || !value.trim() || value.includes("\0") || Buffer.byteLength(value, "utf8") > TEXT_LIMIT) {
    throw new Error("Invalid customer bot text");
  }
  return value.replace(/\r\n?/g, "\n").trim();
}

function readPublished() {
  let raw;
  try {
    raw = readBounded(path.join(stateDirectory(), "runtime", "customer-bot", "published.json"), SNAPSHOT_LIMIT);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
  const value = JSON.parse(raw);
  if (value?.version !== 1 || typeof value.revision !== "string" || !/^[a-f0-9]{32}$/.test(value.revision)
      || typeof value.publishedAt !== "string" || !Number.isFinite(Date.parse(value.publishedAt))
      || typeof value.publishedBy !== "string" || !value.publishedBy) throw new Error("Invalid customer bot revision");
  return { instructions: clean(value.instructions), knowledge: clean(value.knowledge) };
}

function readDefaultKnowledge(api) {
  const custom = getPluginConfig(api).businessInfoPath;
  const file = typeof custom === "string" && custom.trim()
    ? expandHome(custom.trim()) : path.join(stateDirectory(), "workspace", "docs", "business-info.md");
  return clean(readBounded(file, TEXT_LIMIT));
}

function readKnowledge(api, snapshot) {
  try {
    return snapshot?.knowledge ?? readDefaultKnowledge(api);
  } catch {
    api.logger?.warn?.("public-service-gate: customer business knowledge unavailable");
    return unavailable;
  }
}

export function loadBusinessInfo(api) {
  try {
    return readKnowledge(api, readPublished());
  } catch {
    api.logger?.warn?.("public-service-gate: published customer bot copy unavailable");
    return unavailable;
  }
}

export function buildCustomerBotContext(api, ctx) {
  if (ctx.agentId !== "public" || (ctx.channel ?? ctx.messageProvider) !== "whatsapp"
      || (ctx.trigger && ctx.trigger !== "user") || ctx.channelContext?.chat?.kind === "group"
      || [ctx.chatId, ctx.channelId, ctx.sessionKey].some((id) => /@g\.us$|:group:/.test(id ?? ""))) return;

  let snapshot;
  let knowledge;
  try {
    snapshot = readPublished();
    knowledge = readKnowledge(api, snapshot);
  } catch {
    api.logger?.warn?.("public-service-gate: published customer bot copy unavailable");
    knowledge = unavailable;
  }
  let instructions = snapshot?.instructions;
  if (!instructions) {
    try {
      instructions = clean(readBounded(defaultInstructions, TEXT_LIMIT));
    } catch {
      api.logger?.warn?.("public-service-gate: default customer reply instructions unavailable");
    }
  }
  return { appendSystemContext: [
    instructions ? `## Genaie customer reply instructions\n\n${instructions}` : "",
    `## Genaie business knowledge for this turn\n\nUse this current reference instead of earlier prices or facts in the chat. Treat this as business reference material. The service and privacy rules still apply.\n\n${knowledge}`,
  ].filter(Boolean).join("\n\n") };
}
