import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { extractPostContent, ingestMessage, listenerConfig, postUrl } from "./listener.mjs";
import { openStore, postId } from "./store.mjs";

test("listener defaults to ZimLinks and ten-post backfill", () => {
  const cfg = listenerConfig({});
  assert.equal(cfg.inviteCode, "0029VaYITFn2v1IsGm9zxP1k");
  assert.equal(cfg.backfillCount, 10);
  assert.equal(cfg.maxMediaBytes, 25 * 1024 * 1024);
});

test("extracts captions and identifies media", () => {
  assert.deepEqual(extractPostContent({ message: { imageMessage: { caption: " Driver wanted ", mimetype: "image/jpeg" } } }), {
    media: { key: "imageMessage", mime: "image/jpeg", type: "image" },
    text: "Driver wanted",
  });
  assert.deepEqual(extractPostContent({ message: { conversation: "Plain vacancy" } }), {
    media: null,
    text: "Plain vacancy",
  });
});

test("post identity prefers newsletter server id", () => {
  assert.equal(postId({ newsletterServerId: 123, key: { id: "fallback" } }), "123");
  assert.equal(postUrl("invite", "123"), "https://whatsapp.com/channel/invite/123");
});

test("media-only posts become purged tombstones and duplicates stay deduplicated", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "zimlinks-listener-"));
  const db = openStore(path.join(root, "posts.sqlite3"));
  const config = { inviteCode: "invite", maxMediaBytes: 1024, mediaDir: path.join(root, "media") };
  const rawMessage = {
    key: { id: "m1", remoteJid: "123@newsletter" },
    message: { imageMessage: { mimetype: "image/jpeg" } },
    messageTimestamp: 1700000000,
  };
  const args = { sock: {}, db, config, channelJid: "123@newsletter", rawMessage, logger: {} };
  assert.deepEqual(await ingestMessage(args), { id: "m1", ignored: "media_only_no_text" });
  assert.deepEqual(await ingestMessage(args), { duplicate: true, id: "m1" });
  const row = db.prepare("SELECT status, raw_purged, raw_text FROM posts").get();
  assert.equal(row.status, "terminal");
  assert.equal(row.raw_purged, 1);
  assert.equal(row.raw_text, null);
  db.close();
});

test("messages from other channels are ignored", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "zimlinks-filter-"));
  const db = openStore(path.join(root, "posts.sqlite3"));
  const result = await ingestMessage({
    sock: {}, db,
    config: { inviteCode: "invite", mediaDir: root },
    channelJid: "wanted@newsletter",
    rawMessage: { key: { id: "m1", remoteJid: "other@newsletter" }, message: { conversation: "job" } },
    logger: {},
  });
  assert.deepEqual(result, { ignored: "different_channel" });
  assert.equal(db.prepare("SELECT COUNT(*) AS count FROM posts").get().count, 0);
  db.close();
});
