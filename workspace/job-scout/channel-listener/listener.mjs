import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { pipeline } from "node:stream/promises";

import makeWASocket, {
  Browsers,
  DisconnectReason,
  downloadMediaMessage,
  getAllBinaryNodeChildren,
  getBinaryNodeChild,
  proto,
  useMultiFileAuthState,
} from "baileys";
import pino from "pino";
import qrcode from "qrcode-terminal";

import { attachMedia, markTerminal, openStore, postId, setMeta, storeCounts, upsertPost } from "./store.mjs";

const DEFAULT_INVITE_CODE = "0029VaYITFn2v1IsGm9zxP1k";
const DEFAULT_CHANNEL_NAME = "ZimLinks";
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

export function listenerConfig(env = process.env) {
  const dataDir = env.JOB_SCOUT_CHANNEL_DATA_DIR || DEFAULT_DATA_DIR;
  return {
    authDir: env.JOB_SCOUT_CHANNEL_AUTH_DIR || path.join(dataDir, "auth"),
    backfillCount: envInt("JOB_SCOUT_CHANNEL_BACKFILL_COUNT", DEFAULT_BACKFILL_COUNT),
    channelName: env.JOB_SCOUT_CHANNEL_NAME || DEFAULT_CHANNEL_NAME,
    dataDir,
    dbPath: env.JOB_SCOUT_CHANNEL_DB || path.join(dataDir, "posts.sqlite3"),
    healthPath: env.JOB_SCOUT_CHANNEL_HEALTH || path.join(dataDir, "health.json"),
    inviteCode: env.JOB_SCOUT_CHANNEL_INVITE_CODE || DEFAULT_INVITE_CODE,
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
    channelJid: null,
    channelName: null,
    connected: false,
    lastError: null,
    lastHeartbeatAt: null,
    lastSubscriptionAt: null,
    reconnectCount: 0,
    startedAt: new Date().toISOString(),
  };
  let stopped = false;
  let storeClosed = false;
  let socket;
  let renewalTimer;
  let reconnectAttempt = 0;

  const heartbeat = () => {
    if (storeClosed) return;
    health.lastHeartbeatAt = new Date().toISOString();
    atomicJson(config.healthPath, { ...health, ...storeCounts(db) });
  };
  const heartbeatTimer = setInterval(heartbeat, HEARTBEAT_INTERVAL_MS);
  heartbeat();

  async function subscribe(sock, jid) {
    const result = await sock.subscribeNewsletterUpdates(jid);
    health.lastSubscriptionAt = new Date().toISOString();
    const durationSeconds = Number(result?.duration || 0);
    const renewalMs = Math.max(MIN_RENEWAL_MS, Math.floor((durationSeconds || 900) * 800));
    clearTimeout(renewalTimer);
    renewalTimer = setTimeout(() => {
      subscribe(sock, jid).catch((error) => {
        health.lastError = `subscription renewal: ${String(error)}`;
        heartbeat();
      });
    }, renewalMs);
  }

  async function configureChannel(sock) {
    const metadata = await sock.newsletterMetadata("invite", config.inviteCode);
    if (!metadata) throw new Error("channel invite did not resolve");
    const name = metadataName(metadata);
    const jid = metadataJid(metadata);
    if (!jid.endsWith("@newsletter")) throw new Error(`resolved channel has invalid JID: ${jid || "missing"}`);
    if (!name.toLowerCase().includes(config.channelName.toLowerCase())) {
      throw new Error(`resolved channel name mismatch: expected ${config.channelName}, received ${name || "missing"}`);
    }
    await sock.newsletterFollow(jid);
    await subscribe(sock, jid);
    health.channelJid = jid;
    health.channelName = name;
    setMeta(db, "channel", { inviteCode: config.inviteCode, jid, name });
    const result = await sock.newsletterFetchMessages(jid, config.backfillCount, 0, 0);
    const messages = decodeBackfillResponse(result, jid).slice(-config.backfillCount);
    for (const message of messages) {
      await ingestMessage({ sock, db, config, channelJid: jid, rawMessage: message, logger });
    }
    setMeta(db, "last_backfill", { count: messages.length, completedAt: new Date().toISOString() });
    return jid;
  }

  async function connect() {
    if (stopped) return;
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
    });
    socket = sock;
    let channelJid = health.channelJid;
    sock.ev.on("creds.update", saveCreds);
    sock.ev.on("messages.upsert", async (event) => {
      if (!channelJid || !["append", "notify"].includes(event?.type)) return;
      for (const message of event.messages ?? []) {
        try {
          await ingestMessage({ sock, db, config, channelJid, rawMessage: message, logger });
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
        try {
          channelJid = await configureChannel(sock);
          heartbeat();
          if (config.pairing) {
            logger.info("Pairing complete. The ZimLinks listener is ready.");
            setTimeout(stop, 1500);
          }
        } catch (error) {
          health.lastError = `channel setup: ${String(error)}`;
          heartbeat();
          logger.error({ error }, "channel setup failed");
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
    clearTimeout(renewalTimer);
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
    process.stderr.write(`ZimLinks listener failed: ${String(error)}\n`);
    process.exitCode = 1;
  });
}
