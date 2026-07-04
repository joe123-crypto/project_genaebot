import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

export const SCHEMA = `
CREATE TABLE IF NOT EXISTS posts (
  channel_jid TEXT NOT NULL,
  post_id TEXT NOT NULL,
  posted_at TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  post_url TEXT NOT NULL,
  raw_text TEXT,
  media_path TEXT,
  media_mime TEXT,
  media_sha256 TEXT,
  media_bytes INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  candidate_json TEXT,
  expires_at TEXT,
  parse_attempts INTEGER NOT NULL DEFAULT 0,
  parse_error TEXT,
  model_used TEXT,
  raw_purged INTEGER NOT NULL DEFAULT 0,
  terminal_at TEXT,
  tombstone_until TEXT,
  PRIMARY KEY (channel_jid, post_id)
);
CREATE INDEX IF NOT EXISTS posts_status_posted_idx ON posts(status, posted_at DESC);
CREATE TABLE IF NOT EXISTS post_eligibility (
  run_id TEXT NOT NULL,
  channel_jid TEXT NOT NULL,
  post_id TEXT NOT NULL,
  user_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, channel_jid, post_id, user_key)
);
CREATE TABLE IF NOT EXISTS post_outcomes (
  run_id TEXT NOT NULL,
  channel_jid TEXT NOT NULL,
  post_id TEXT NOT NULL,
  user_key TEXT NOT NULL,
  outcome TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, channel_jid, post_id, user_key)
);
CREATE TABLE IF NOT EXISTS listener_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
`;

export function openStore(dbPath) {
  fs.mkdirSync(path.dirname(dbPath), { recursive: true, mode: 0o700 });
  const db = new DatabaseSync(dbPath);
  db.exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;");
  db.exec(SCHEMA);
  return db;
}

export function postId(message) {
  const raw = message?.newsletterServerId ?? message?.server_id ?? message?.key?.id;
  if (raw !== undefined && raw !== null && String(raw).trim()) return String(raw).trim();
  return crypto.createHash("sha256").update(JSON.stringify(message?.message ?? message ?? {})).digest("hex").slice(0, 24);
}

function nowIso() {
  return new Date().toISOString();
}

export function upsertPost(db, post) {
  const now = nowIso();
  const existing = db.prepare(
    "SELECT status, media_path AS mediaPath FROM posts WHERE channel_jid = ? AND post_id = ?",
  ).get(post.channelJid, post.postId);
  if (existing) {
    db.prepare("UPDATE posts SET last_seen_at = ? WHERE channel_jid = ? AND post_id = ?").run(
      now,
      post.channelJid,
      post.postId,
    );
    return { inserted: false, existing };
  }
  db.prepare(`
    INSERT INTO posts (
      channel_jid, post_id, posted_at, first_seen_at, last_seen_at,
      post_url, raw_text, status, raw_purged
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
  `).run(
    post.channelJid,
    post.postId,
    post.postedAt ?? null,
    now,
    now,
    post.postUrl,
    post.text || null,
    post.status ?? "pending",
    post.rawPurged ? 1 : 0,
  );
  return { inserted: true };
}

export function attachMedia(db, channelJid, id, media) {
  db.prepare(`
    UPDATE posts
       SET media_path = ?, media_mime = ?, media_sha256 = ?, media_bytes = ?
     WHERE channel_jid = ? AND post_id = ?
  `).run(media.path, media.mime ?? null, media.sha256, media.bytes, channelJid, id);
}

export function markTerminal(db, channelJid, id, reason) {
  const now = new Date();
  const tombstone = new Date(now.getTime() + 30 * 24 * 60 * 60 * 1000).toISOString();
  db.prepare(`
    UPDATE posts
       SET status = 'terminal', raw_text = NULL, raw_purged = 1,
           terminal_at = ?, tombstone_until = ?, parse_error = ?
     WHERE channel_jid = ? AND post_id = ?
  `).run(now.toISOString(), tombstone, reason, channelJid, id);
}

export function setMeta(db, key, value) {
  const serialized = typeof value === "string" ? value : JSON.stringify(value);
  db.prepare(`
    INSERT INTO listener_meta(key, value, updated_at) VALUES (?, ?, ?)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
  `).run(key, serialized, nowIso());
}

export function storeCounts(db) {
  const rows = db.prepare("SELECT status, COUNT(*) AS count FROM posts GROUP BY status").all();
  const counts = Object.fromEntries(rows.map((row) => [String(row.status), Number(row.count)]));
  const media = db.prepare("SELECT COALESCE(SUM(media_bytes), 0) AS bytes FROM posts WHERE raw_purged = 0").get();
  return { counts, mediaBytes: Number(media?.bytes ?? 0) };
}
