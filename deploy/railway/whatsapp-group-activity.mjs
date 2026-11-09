// Passive socket observations. Never dispatches messages or changes targeting.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
export const HEARTBEAT_MS = 30_000;
export const MAX_HEARTBEAT_GAP_MS = 90_000;
export const RETENTION_MS = 48 * 3600_000;
const GROUP = /^\d+(?:-\d+)?@g\.us$/;
const ACCOUNT = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const owners = new Map();
const rootPath = () => process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw');
const ownerKey = (root, accountId) => `${path.resolve(root)}:${accountId}`;
const kinds = new Set(['conversation', 'extendedTextMessage', 'imageMessage', 'videoMessage',
  'audioMessage', 'documentMessage', 'stickerMessage', 'contactMessage', 'contactsArrayMessage',
  'locationMessage', 'liveLocationMessage', 'pollCreationMessage', 'pollCreationMessageV2', 'pollCreationMessageV3']);
const wrappers = ['ephemeralMessage', 'viewOnceMessage', 'viewOnceMessageV2',
  'viewOnceMessageV2Extension', 'documentWithCaptionMessage'];

export function identityHash(value) {
  if (typeof value !== 'string') return null;
  const match = /^(\d+)(?::\d+)?@(s\.whatsapp\.net|c\.us|hosted|lid|hosted\.lid)$/.exec(value);
  if (!match) return null;
  const server = ['c.us', 'hosted'].includes(match[2]) ? 's.whatsapp.net' : match[2] === 'hosted.lid' ? 'lid' : match[2];
  const normalized = `${match[1]}@${server}`;
  return createHash('sha256').update(normalized).digest('hex');
}

function isOriginalMessage(message) {
  let content = message;
  for (let depth = 0; depth < 8 && content; depth++) {
    if (content.protocolMessage || content.reactionMessage || content.editedMessage) return false;
    const wrapper = wrappers.find(key => content[key]?.message);
    if (wrapper) { content = content[wrapper].message; continue; }
    return Object.keys(content).some(key => kinds.has(key));
  }
  return false;
}

/** Discovery shares only group IDs with the independently owned collector. */
export function observeWhatsAppGroups(accountId, groups, root = rootPath()) {
  owners.get(ownerKey(root, accountId))?.observeGroups(groups);
}

export function createWhatsAppGroupActivity({ accountId, root = rootPath(), environment = process.env,
  now = Date.now, warn = console.warn, setIntervalFn = setInterval, clearIntervalFn = clearInterval } = {}) {
  const enabled = environment.GENAIE_GROUP_ACTIVITY !== '0' && ACCOUNT.test(accountId ?? '') &&
    !['__proto__', 'constructor', 'prototype'].includes(accountId);
  const key = ownerKey(root, accountId);
  let database, socket, timer, runId, lastHeartbeat, failed = false, warned = false;
  let groups = new Set();

  function report() {
    failed = true;
    if (!warned) { warned = true; warn(`[whatsapp-activity] ${accountId}: collection unavailable; counts may be partial`); }
  }

  function transaction(task) {
    database.exec('BEGIN IMMEDIATE');
    try { task(); database.exec('COMMIT'); }
    catch (error) { database.exec('ROLLBACK'); throw error; }
  }

  function beginSegment(stamp) {
    runId = randomUUID();
    lastHeartbeat = stamp;
    failed = false;
  }

  function checkpoint(stamp) {
    // Never bridge a storage failure, event-loop stall or crash with coverage.
    if (failed || stamp - lastHeartbeat > MAX_HEARTBEAT_GAP_MS || stamp < lastHeartbeat) {
      database.prepare('UPDATE coverage SET live = 0 WHERE run_id = ?').run(runId);
      beginSegment(stamp);
    }
    const insert = database.prepare('INSERT OR IGNORE INTO coverage VALUES (?, ?, ?, ?, 1)');
    for (const jid of groups) insert.run(runId, jid, stamp, stamp);
    database.prepare('UPDATE coverage SET through_at = ? WHERE run_id = ? AND live = 1').run(stamp, runId);
    lastHeartbeat = stamp;
  }

  function heartbeat() {
    if (!database || owners.get(key) !== api) return;
    try {
      transaction(() => {
        const stamp = now();
        checkpoint(stamp);
        database.prepare('DELETE FROM events WHERE message_at < ?').run(stamp - RETENTION_MS);
        database.prepare('DELETE FROM own_messages WHERE message_at < ?').run(stamp - RETENTION_MS);
        database.prepare('DELETE FROM coverage WHERE through_at < ?').run(stamp - RETENTION_MS);
        database.prepare('UPDATE coverage SET started_at = ? WHERE started_at < ?')
          .run(stamp - RETENTION_MS, stamp - RETENTION_MS);
      });
    } catch { report(); }
  }

  function rememberIdentity(value) {
    const hash = identityHash(value);
    if (!hash) return;
    const added = database.prepare('INSERT OR IGNORE INTO bot_identities VALUES (?)').run(hash);
    // A sibling line may deliver a copy before this account starts monitoring.
    if (added.changes > 0) database.prepare('DELETE FROM events WHERE sender_hash = ? OR sender_alt_hash = ?').run(hash, hash);
  }

  function receive(batch) {
    if (!database || owners.get(key) !== api || batch?.type !== 'notify' || !Array.isArray(batch.messages)) return;
    try {
      transaction(() => {
        const stamp = now();
        checkpoint(stamp);
        rememberIdentity(socket?.user?.id);
        rememberIdentity(socket?.user?.lid);
        for (const message of batch.messages) {
          const jid = message?.key?.remoteJid, id = message?.key?.id;
          if (typeof jid !== 'string' || !GROUP.test(jid) || jid.length > 128 ||
              typeof id !== 'string' || !id.length || id.length > 256) continue;
          const seconds = Number(message.messageTimestamp);
          const timestamp = seconds * 1000;
          if (!Number.isSafeInteger(timestamp) || timestamp <= stamp - RETENTION_MS || timestamp > stamp) continue;
          if (message.key.fromMe === true) {
            database.prepare('INSERT OR IGNORE INTO own_messages VALUES (?, ?, ?)').run(jid, id, timestamp);
            database.prepare('DELETE FROM events WHERE jid = ? AND message_id = ?').run(jid, id);
            continue;
          }
          if (message.messageStubType != null || !isOriginalMessage(message.message)) continue;
          const sender = identityHash(message.key.participant ?? message.participant);
          const alternate = identityHash(message.key.participantAlt);
          if (!sender && !alternate) continue;
          if (database.prepare('SELECT 1 FROM bot_identities WHERE hash IN (?, ?)').get(sender, alternate) ||
              database.prepare('SELECT 1 FROM own_messages WHERE jid = ? AND message_id = ?').get(jid, id)) continue;
          database.prepare('INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?)')
            .run(jid, id, timestamp, sender, alternate);
          if (!groups.has(jid)) {
            database.prepare('UPDATE coverage SET live = 0 WHERE run_id = ?').run(runId);
            beginSegment(stamp);
            groups.add(jid);
            checkpoint(stamp);
          }
        }
      });
    } catch { report(); }
  }

  function observeGroups(metadata) {
    if (!database || owners.get(key) !== api || !metadata || Array.isArray(metadata)) return;
    try {
      transaction(() => {
        const stamp = now();
        checkpoint(stamp);
        const next = new Set(Object.keys(metadata).filter(jid => GROUP.test(jid) && jid.length <= 128));
        for (const jid of groups) {
          if (!next.has(jid)) database.prepare('UPDATE coverage SET live = 0 WHERE run_id = ? AND jid = ?').run(runId, jid);
        }
        // Give rejoined groups their own segment; INSERT OR IGNORE must not
        // stitch a membership gap into one continuous interval.
        if ([...next].some(jid => !groups.has(jid))) {
          database.prepare('UPDATE coverage SET live = 0 WHERE run_id = ?').run(runId);
          beginSegment(stamp);
        }
        groups = next;
        checkpoint(stamp);
      });
    } catch { report(); }
  }

  function stop() {
    if (timer !== undefined) clearIntervalFn(timer);
    timer = undefined;
    socket?.ev?.off?.('messages.upsert', receive);
    socket?.ev?.off?.('connection.update', connectionUpdate);
    if (database) {
      try {
        transaction(() => {
          if (!failed && now() - lastHeartbeat <= MAX_HEARTBEAT_GAP_MS) checkpoint(now());
          database.prepare('UPDATE coverage SET live = 0 WHERE run_id = ?').run(runId);
        });
      } catch { report(); }
      try { database.close(); } catch { /* Telemetry cannot stop the channel. */ }
    }
    database = socket = undefined;
    groups = new Set();
    if (owners.get(key) === api) owners.delete(key);
  }

  function connectionUpdate(update) {
    if (update?.connection === 'close') stop();
  }

  function start(sock) {
    stop();
    if (!enabled) return;
    if (typeof sock?.ev?.on !== 'function' || typeof sock.ev.off !== 'function') { report(); return; }
    owners.get(key)?.stop();
    try {
      const directory = path.join(root, 'runtime');
      fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
      const filename = path.join(directory, 'whatsapp-activity.sqlite');
      // Create privately before SQLite opens it; metadata never belongs in a
      // world-readable file even on hosts with a permissive umask.
      fs.closeSync(fs.openSync(filename, 'a', 0o600));
      const { DatabaseSync } = require('node:sqlite');
      database = new DatabaseSync(filename);
      // DELETE journaling lets the bridge use mode=ro without ever creating
      // WAL/SHM sidecars when the gateway is offline. Readers hold a short snapshot.
      database.exec(`PRAGMA busy_timeout = 100; PRAGMA journal_mode = DELETE;
        CREATE TABLE IF NOT EXISTS events (jid TEXT, message_id TEXT, message_at INTEGER,
          sender_hash TEXT, sender_alt_hash TEXT, PRIMARY KEY (jid, message_id));
        CREATE INDEX IF NOT EXISTS events_time ON events(message_at);
        CREATE TABLE IF NOT EXISTS own_messages (jid TEXT, message_id TEXT, message_at INTEGER, PRIMARY KEY (jid, message_id));
        CREATE TABLE IF NOT EXISTS bot_identities (hash TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS coverage (run_id TEXT, jid TEXT, started_at INTEGER,
          through_at INTEGER, live INTEGER, PRIMARY KEY (run_id, jid));`);
      socket = sock;
      beginSegment(now());
      transaction(() => { rememberIdentity(sock.user?.id); rememberIdentity(sock.user?.lid); });
      owners.set(key, api);
      sock.ev.on('messages.upsert', receive);
      sock.ev.on('connection.update', connectionUpdate);
      heartbeat();
      timer = setIntervalFn(heartbeat, HEARTBEAT_MS);
      timer?.unref?.();
    } catch { report(); stop(); }
  }

  const api = { start, stop, observeGroups, heartbeat };
  return api;
}
