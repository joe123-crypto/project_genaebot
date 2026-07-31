import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { pipeline } from "node:stream/promises";

import makeWASocket, {
  Browsers,
  DEFAULT_CONNECTION_CONFIG,
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  getAllBinaryNodeChildren,
  getBinaryNodeChild,
  proto,
  useMultiFileAuthState,
} from "baileys";
import pino from "pino";
import qrcode from "qrcode-terminal";

import { attachMedia, markTerminal, openStore, postId, setMeta, storeCounts, upsertPost } from "./store.mjs";

const DEFAULT_CHANNELS = [{
  inviteCode: "0029VaYITFn2v1IsGm9zxP1k",
  expectedName: "ZimLinks",
  source: "zimlinks-whatsapp",
}];
const DEFAULT_DATA_DIR = "/data";
const DEFAULT_MAX_MEDIA_BYTES = 25 * 1024 * 1024;
const DEFAULT_BACKFILL_COUNT = 10;
const HEARTBEAT_INTERVAL_MS = 30_000;
const MIN_RENEWAL_MS = 60_000;
const MAX_RECONNECT_MS = 60_000;

function envInt(name, fallback) {
  const value = Number.parseInt(process.env[name] ?? "", 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function validWhatsAppVersion(value) {
  return Array.isArray(value)
    && value.length === 3
    && value.every((part) => Number.isInteger(part) && part >= 0);
}

export async function resolveWhatsAppVersion(
  fetchVersion = fetchLatestBaileysVersion,
  fallbackVersion = DEFAULT_CONNECTION_CONFIG.version,
) {
  if (!validWhatsAppVersion(fallbackVersion)) throw new Error("Baileys fallback WhatsApp version is invalid");
  try {
    const result = await fetchVersion();
    if (!validWhatsAppVersion(result?.version)) throw new Error("resolved WhatsApp version is invalid");
    return { source: "latest", version: [...result.version] };
  } catch (error) {
    return { error, source: "fallback", version: [...fallbackVersion] };
  }
}

function channelConfig(env) {
  let values;
  if (env.JOB_SCOUT_CHANNELS_JSON) {
    try {
      values = JSON.parse(env.JOB_SCOUT_CHANNELS_JSON);
    } catch (error) {
      throw new Error(`JOB_SCOUT_CHANNELS_JSON is invalid JSON: ${String(error)}`);
    }
  } else {
    values = [{
      inviteCode: env.JOB_SCOUT_CHANNEL_INVITE_CODE || DEFAULT_CHANNELS[0].inviteCode,
      expectedName: env.JOB_SCOUT_CHANNEL_NAME || DEFAULT_CHANNELS[0].expectedName,
      source: DEFAULT_CHANNELS[0].source,
    }];
  }
  if (!Array.isArray(values) || values.length === 0) {
    throw new Error("JOB_SCOUT_CHANNELS_JSON must be a non-empty array");
  }
  const inviteCodes = new Set();
  const sources = new Set();
  return values.map((value, index) => {
    const inviteCode = String(value?.inviteCode || "").trim();
    const expectedName = String(value?.expectedName || "").trim();
    const source = String(value?.source || "").trim();
    if (!/^[A-Za-z0-9_-]+$/.test(inviteCode)) throw new Error(`channel ${index + 1} has an invalid inviteCode`);
    if (!expectedName) throw new Error(`channel ${index + 1} is missing expectedName`);
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(source)) throw new Error(`channel ${index + 1} has an invalid source`);
    if (inviteCodes.has(inviteCode)) throw new Error(`duplicate channel inviteCode: ${inviteCode}`);
    if (sources.has(source)) throw new Error(`duplicate channel source: ${source}`);
    inviteCodes.add(inviteCode);
    sources.add(source);
    return { inviteCode, expectedName, source };
  });
}

export function listenerConfig(env = process.env) {
  const dataDir = env.JOB_SCOUT_CHANNEL_DATA_DIR || DEFAULT_DATA_DIR;
  return {
    authDir: env.JOB_SCOUT_CHANNEL_AUTH_DIR || path.join(dataDir, "auth"),
    backfillCount: envInt("JOB_SCOUT_CHANNEL_BACKFILL_COUNT", DEFAULT_BACKFILL_COUNT),
    channels: channelConfig(env),
    dataDir,
    dbPath: env.JOB_SCOUT_CHANNEL_DB || path.join(dataDir, "posts.sqlite3"),
    healthPath: env.JOB_SCOUT_CHANNEL_HEALTH || path.join(dataDir, "health.json"),
    maxMediaBytes: envInt("JOB_SCOUT_CHANNEL_MAX_MEDIA_BYTES", DEFAULT_MAX_MEDIA_BYTES),
    mediaDir: env.JOB_SCOUT_CHANNEL_MEDIA_DIR || path.join(dataDir, "media"),
    pairing: env.JOB_SCOUT_CHANNEL_PAIRING === "1",
  };
}

function atomicJson(pathname, value) {
  fs.mkdirSync(path.dirname(pathname), { recursive: true, mode: 0o700 });
  const temp = `${pathname}.${process.pid}.tmp`;
  fs.writeFileSync(temp, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temp, pathname);
}

function safeLong(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === "object" && typeof value.toString === "function") return value.toString();
  return String(value);
}

function timestampIso(value) {
  const raw = safeLong(value);
  if (!raw || !/^\d+$/.test(raw)) return null;
  const numeric = Number(raw);
  if (!Number.isFinite(numeric)) return null;
  return new Date((numeric > 10_000_000_000 ? numeric : numeric * 1000)).toISOString();
}

function unwrapMessage(message) {
  let current = message;
  const wrappers = [
    "ephemeralMessage",
    "viewOnceMessage",
    "viewOnceMessageV2",
    "viewOnceMessageV2Extension",
    "documentWithCaptionMessage",
  ];
  for (let depth = 0; depth < 6 && current; depth += 1) {
    let next;
    for (const key of wrappers) {
      if (current[key]?.message) {
        next = current[key].message;
        break;
      }
    }
    if (!next) break;
    current = next;
  }
  return current ?? {};
}

export function extractPostContent(rawMessage) {
  const message = unwrapMessage(rawMessage?.message ?? {});
  const text = [
    message.conversation,
    message.extendedTextMessage?.text,
    message.imageMessage?.caption,
    message.videoMessage?.caption,
    message.documentMessage?.caption,
  ].find((value) => typeof value === "string" && value.trim())?.trim() ?? "";
  for (const [key, type] of [
    ["imageMessage", "image"],
    ["videoMessage", "video"],
    ["documentMessage", "document"],
    ["audioMessage", "audio"],
    ["stickerMessage", "sticker"],
  ]) {
    if (!message[key]) continue;
    return {
      media: { key, mime: message[key].mimetype ?? null, type },
      text,
    };
  }
  return { media: null, text };
}

function messageFromFetchNode(node, channelJid) {
  const plaintext = getBinaryNodeChild(node, "plaintext");
  if (!plaintext?.content) return null;
  const content = typeof plaintext.content === "string"
    ? Buffer.from(plaintext.content, "binary")
    : Buffer.from(plaintext.content);
  const decoded = proto.Message.decode(content);
  return {
    key: {
      remoteJid: channelJid,
      id: node.attrs?.message_id || node.attrs?.server_id,
      fromMe: false,
    },
    message: decoded,
    messageTimestamp: Number(node.attrs?.t || 0),
    newsletterServerId: node.attrs?.server_id || node.attrs?.message_id,
  };
}

export function decodeBackfillResponse(result, channelJid) {
  const found = [];
  const visit = (node) => {
    if (!node || typeof node !== "object") return;
    if (node.tag === "message") {
      const decoded = messageFromFetchNode(node, channelJid);
      if (decoded) found.push(decoded);
    }
    for (const child of getAllBinaryNodeChildren(node)) visit(child);
  };
  visit(result);
  return found;
}

export async function fetchBackfillMessages(sock, channelJid, count) {
  // Omitting `since` asks WhatsApp for the latest posts. Passing zero sends an
  // epoch cursor and can cause the newsletter query to time out without data.
  const result = await sock.newsletterFetchMessages(channelJid, count, undefined, 0);
  return decodeBackfillResponse(result, channelJid).slice(-count);
}

function extensionFor(media) {
  const mime = String(media?.mime || "").toLowerCase();
  const subtype = mime.split("/", 2)[1]?.split(";", 1)[0]?.replace(/[^a-z0-9.+-]/g, "");
  if (subtype) return subtype === "jpeg" ? "jpg" : subtype;
  return ({ image: "jpg", video: "mp4", audio: "ogg", sticker: "webp", document: "bin" })[media?.type] || "bin";
}

async function saveMedia(sock, rawMessage, config, id, media, logger) {
  fs.mkdirSync(config.mediaDir, { recursive: true, mode: 0o700 });
  const finalPath = path.join(config.mediaDir, `${id.replace(/[^a-zA-Z0-9._-]/g, "_")}.${extensionFor(media)}`);
  const tempPath = `${finalPath}.${process.pid}.tmp`;
  const output = fs.createWriteStream(tempPath, { flags: "wx", mode: 0o600 });
  const hash = crypto.createHash("sha256");
  let bytes = 0;
  const stream = await downloadMediaMessage(rawMessage, "stream", {}, {
    logger,
    reuploadRequest: sock.updateMediaMessage,
  });
  stream.on("data", (chunk) => {
    bytes += chunk.length;
    hash.update(chunk);
    if (bytes > config.maxMediaBytes) stream.destroy(new Error("media exceeds configured limit"));
  });
  try {
    await pipeline(stream, output);
    fs.renameSync(tempPath, finalPath);
  } catch (error) {
    fs.rmSync(tempPath, { force: true });
    throw error;
  }
  return { bytes, mime: media.mime, path: finalPath, sha256: hash.digest("hex") };
}

export function postUrl(inviteCode, id) {
  return `https://whatsapp.com/channel/${encodeURIComponent(inviteCode)}/${encodeURIComponent(id)}`;
}

export async function ingestMessage({ sock, db, config, channelJid, rawMessage, logger }) {
  if (rawMessage?.key?.remoteJid !== channelJid) return { ignored: "different_channel" };
  const id = postId(rawMessage);
  const content = extractPostContent(rawMessage);
  const mediaOnly = !content.text && Boolean(content.media);
  const claim = upsertPost(db, {
    channelJid,
    postId: id,
    postedAt: timestampIso(rawMessage.messageTimestamp),
    postUrl: postUrl(config.inviteCode, id),
    rawPurged: mediaOnly,
    status: mediaOnly ? "terminal" : "pending",
    text: content.text,
  });
  if (!claim.inserted) return { duplicate: true, id };
  if (mediaOnly) {
    markTerminal(db, channelJid, id, "media_only_no_text");
    return { id, ignored: "media_only_no_text" };
  }
  if (content.media) {
    try {
      attachMedia(db, channelJid, id, await saveMedia(sock, rawMessage, config, id, content.media, logger));
    } catch (error) {
      db.prepare("UPDATE posts SET parse_error = ? WHERE channel_jid = ? AND post_id = ?").run(
        `media_capture: ${String(error)}`.slice(0, 500),
        channelJid,
        id,
      );
    }
  }
  setMeta(db, "last_post", { id, receivedAt: new Date().toISOString() });
  return { captured: true, id };
}

function metadataName(metadata) {
  return String(metadata?.name || metadata?.thread_metadata?.name?.text || "").trim();
}

function metadataJid(metadata) {
  return String(metadata?.id || metadata?.jid || metadata?.thread_metadata?.id || "").trim();
}

function disconnectStatus(update) {
  return update?.lastDisconnect?.error?.output?.statusCode
    ?? update?.lastDisconnect?.error?.statusCode
    ?? null;
}

export async function runListener(config = listenerConfig()) {
  fs.mkdirSync(config.authDir, { recursive: true, mode: 0o700 });
  fs.mkdirSync(config.mediaDir, { recursive: true, mode: 0o700 });
  const db = openStore(config.dbPath);
  const logger = pino({ level: process.env.LOG_LEVEL || "info" });
  const health = {
    authenticated: false,
    channels: config.channels.map((channel) => ({
      channelJid: null,
      channelName: null,
      expectedName: channel.expectedName,
      lastError: null,
      lastSubscriptionAt: null,
      source: channel.source,
    })),
    channelJid: null,
    channelName: null,
    connected: false,
    lastError: null,
    lastHeartbeatAt: null,
    lastSubscriptionAt: null,
    reconnectCount: 0,
    startedAt: new Date().toISOString(),
    whatsappVersion: null,
    whatsappVersionSource: null,
  };
  let stopped = false;
  let storeClosed = false;
  let socket;
  let cachedWhatsAppVersion;
  const renewalTimers = new Map();
  let reconnectAttempt = 0;

  const heartbeat = () => {
    if (storeClosed) return;
    health.lastHeartbeatAt = new Date().toISOString();
    atomicJson(config.healthPath, { ...health, ...storeCounts(db) });
  };
  const heartbeatTimer = setInterval(heartbeat, HEARTBEAT_INTERVAL_MS);
  heartbeat();

  async function subscribe(sock, channelHealth, jid) {
    const result = await sock.subscribeNewsletterUpdates(jid);
    channelHealth.lastSubscriptionAt = new Date().toISOString();
    channelHealth.lastError = null;
    if (channelHealth === health.channels[0]) health.lastSubscriptionAt = channelHealth.lastSubscriptionAt;
    const durationSeconds = Number(result?.duration || 0);
    const renewalMs = Math.max(MIN_RENEWAL_MS, Math.floor((durationSeconds || 900) * 800));
    clearTimeout(renewalTimers.get(jid));
    renewalTimers.set(jid, setTimeout(() => {
      subscribe(sock, channelHealth, jid).catch((error) => {
        channelHealth.lastError = `subscription renewal: ${String(error)}`;
        health.lastError = `${channelHealth.source}: ${channelHealth.lastError}`;
        heartbeat();
      });
    }, renewalMs));
  }

  async function configureChannel(sock, channel, channelHealth) {
    const metadata = await sock.newsletterMetadata("invite", channel.inviteCode);
    if (!metadata) throw new Error("channel invite did not resolve");
    const name = metadataName(metadata);
    const jid = metadataJid(metadata);
    if (!jid.endsWith("@newsletter")) throw new Error(`resolved channel has invalid JID: ${jid || "missing"}`);
    if (!name.toLowerCase().includes(channel.expectedName.toLowerCase())) {
      throw new Error(`resolved channel name mismatch: expected ${channel.expectedName}, received ${name || "missing"}`);
    }
    await sock.newsletterFollow(jid);
    await subscribe(sock, channelHealth, jid);
    channelHealth.channelJid = jid;
    channelHealth.channelName = name;
    channelHealth.lastError = null;
    if (channelHealth === health.channels[0]) {
      health.channelJid = jid;
      health.channelName = name;
      setMeta(db, "channel", { inviteCode: channel.inviteCode, jid, name, source: channel.source });
    }
    setMeta(db, `channel:${jid}`, { inviteCode: channel.inviteCode, jid, name, source: channel.source });
    const messages = await fetchBackfillMessages(sock, jid, config.backfillCount);
    const ingestConfig = { ...config, ...channel };
    for (const message of messages) {
      await ingestMessage({ sock, db, config: ingestConfig, channelJid: jid, rawMessage: message, logger });
    }
    setMeta(db, `last_backfill:${jid}`, { count: messages.length, completedAt: new Date().toISOString() });
    return { ...channel, jid, name };
  }

  async function connect() {
    if (stopped) return;
    let versionResolution = cachedWhatsAppVersion;
    if (!versionResolution) {
      versionResolution = await resolveWhatsAppVersion();
      if (versionResolution.source === "latest") {
        cachedWhatsAppVersion = versionResolution;
        logger.info(
          { version: versionResolution.version.join(".") },
          "resolved current WhatsApp Web version",
        );
      } else {
        logger.warn(
          { error: String(versionResolution.error), version: versionResolution.version.join(".") },
          "could not resolve current WhatsApp Web version; using Baileys fallback",
        );
      }
    }
    health.whatsappVersion = versionResolution.version.join(".");
    health.whatsappVersionSource = versionResolution.source;
    heartbeat();
    const { state, saveCreds } = await useMultiFileAuthState(config.authDir);
    const sock = makeWASocket({
      auth: state,
      browser: Browsers.ubuntu("Genaie Job Scout Channel"),
      connectTimeoutMs: 60_000,
      defaultQueryTimeoutMs: 60_000,
      keepAliveIntervalMs: 25_000,
      logger,
      markOnlineOnConnect: false,
      syncFullHistory: false,
      version: versionResolution.version,
    });
    socket = sock;
    const channelsByJid = new Map();
    sock.ev.on("creds.update", saveCreds);
    sock.ev.on("messages.upsert", async (event) => {
      if (!["append", "notify"].includes(event?.type)) return;
      for (const message of event.messages ?? []) {
        const channel = channelsByJid.get(message?.key?.remoteJid);
        if (!channel) continue;
        try {
          await ingestMessage({
            sock, db,
            config: { ...config, ...channel },
            channelJid: channel.jid,
            rawMessage: message,
            logger,
          });
          heartbeat();
        } catch (error) {
          health.lastError = `message ingestion: ${String(error)}`;
          heartbeat();
        }
      }
    });
    sock.ev.on("connection.update", async (update) => {
      if (update.qr) {
        if (config.pairing) qrcode.generate(update.qr, { small: true });
        else health.lastError = "authentication required; run the pairing command";
        heartbeat();
      }
      if (update.connection === "open") {
        reconnectAttempt = 0;
        health.authenticated = true;
        health.connected = true;
        health.lastError = null;
        health.channelJid = null;
        health.channelName = null;
        health.lastSubscriptionAt = null;
        channelsByJid.clear();
        for (const [index, channel] of config.channels.entries()) {
          const channelHealth = health.channels[index];
          channelHealth.channelJid = null;
          channelHealth.channelName = null;
          channelHealth.lastError = null;
          channelHealth.lastSubscriptionAt = null;
          try {
            const resolved = await configureChannel(sock, channel, channelHealth);
            channelsByJid.set(resolved.jid, resolved);
          } catch (error) {
            channelHealth.lastError = String(error);
            health.lastError = `${channel.source}: ${String(error)}`;
            logger.error({ error, source: channel.source }, "channel setup failed");
          }
        }
        heartbeat();
        if (config.pairing && channelsByJid.size === config.channels.length) {
          logger.info("Pairing complete. The Job Scout channel listener is ready.");
          setTimeout(stop, 1500);
        }
      }
      if (update.connection === "close") {
        if (stopped) return;
        health.connected = false;
        health.reconnectCount += 1;
        const status = disconnectStatus(update);
        const loggedOut = status === DisconnectReason.loggedOut;
        health.authenticated = !loggedOut;
        health.lastError = loggedOut ? "WhatsApp session logged out; pairing required" : `connection closed (${status ?? "unknown"})`;
        heartbeat();
        if (!stopped && !loggedOut) {
          reconnectAttempt += 1;
          const delay = Math.min(MAX_RECONNECT_MS, 1000 * (2 ** Math.min(reconnectAttempt, 6)));
          setTimeout(() => connect().catch((error) => {
            health.lastError = `reconnect: ${String(error)}`;
            heartbeat();
          }), delay);
        }
      }
    });
  }

  const stop = () => {
    if (stopped) return;
    stopped = true;
    clearInterval(heartbeatTimer);
    for (const timer of renewalTimers.values()) clearTimeout(timer);
    renewalTimers.clear();
    health.connected = false;
    heartbeat();
    try { socket?.end?.(); } catch {}
    setTimeout(() => {
      if (storeClosed) return;
      storeClosed = true;
      db.close();
    }, 1000);
  };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  await connect();
  return { db, health, stop };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  runListener().catch((error) => {
    process.stderr.write(`Job Scout channel listener failed: ${String(error)}\n`);
    process.exitCode = 1;
  });
}
