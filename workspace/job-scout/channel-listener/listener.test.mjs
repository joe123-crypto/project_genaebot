import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  extractPostContent,
  fetchBackfillMessages,
  ingestMessage,
  listenerConfig,
  postUrl,
  resolveWhatsAppVersion,
} from "./listener.mjs";
import { openStore, postId } from "./store.mjs";

test("listener defaults to ZimLinks and ten-post backfill", () => {
  const cfg = listenerConfig({});
  assert.deepEqual(cfg.channels, [{
    inviteCode: "0029VaYITFn2v1IsGm9zxP1k",
    expectedName: "ZimLinks",
    source: "zimlinks-whatsapp",
  }]);
  assert.equal(cfg.backfillCount, 10);
  assert.equal(cfg.maxMediaBytes, 25 * 1024 * 1024);
});

test("listener accepts two distinct channel definitions", () => {
  const channels = [
    { inviteCode: "first", expectedName: "First", source: "first-whatsapp" },
    { inviteCode: "second", expectedName: "Second", source: "second-whatsapp" },
  ];
  assert.deepEqual(listenerConfig({ JOB_SCOUT_CHANNELS_JSON: JSON.stringify(channels) }).channels, channels);
  assert.throws(
    () => listenerConfig({ JOB_SCOUT_CHANNELS_JSON: JSON.stringify([channels[0], { ...channels[1], source: channels[0].source }]) }),
    /duplicate channel source/,
  );
});

test("resolves and validates the current WhatsApp Web version", async () => {
  const resolved = await resolveWhatsAppVersion(async () => ({
    isLatest: true,
    version: [2, 3000, 1043857760],
  }));
  assert.deepEqual(resolved, {
    source: "latest",
    version: [2, 3000, 1043857760],
  });
});

test("uses the controlled fallback when WhatsApp version lookup fails", async () => {
  const failed = await resolveWhatsAppVersion(
    async () => { throw new Error("version service unavailable"); },
    [2, 3000, 1035194821],
  );
  assert.equal(failed.source, "fallback");
  assert.deepEqual(failed.version, [2, 3000, 1035194821]);
  assert.match(String(failed.error), /version service unavailable/);

  const invalid = await resolveWhatsAppVersion(
    async () => ({ version: [2, "invalid", 1] }),
    [2, 3000, 1035194821],
  );
  assert.equal(invalid.source, "fallback");
  assert.match(String(invalid.error), /resolved WhatsApp version is invalid/);
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

test("backfill requests latest posts without an epoch cursor", async () => {
  const calls = [];
  const sock = {
    newsletterFetchMessages: async (...args) => {
      calls.push(args);
      return null;
    },
  };
  assert.deepEqual(await fetchBackfillMessages(sock, "123@newsletter", 10), []);
  assert.deepEqual(calls, [["123@newsletter", 10, undefined, 0]]);
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

test("the same post id from two channels retains distinct provenance", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "job-channels-provenance-"));
  const db = openStore(path.join(root, "posts.sqlite3"));
  for (const [channelJid, inviteCode] of [["one@newsletter", "first"], ["two@newsletter", "second"]]) {
    await ingestMessage({
      sock: {}, db,
      config: { inviteCode, mediaDir: root },
      channelJid,
      rawMessage: { key: { id: "same", remoteJid: channelJid }, message: { conversation: "VACANCY: Driver" } },
      logger: {},
    });
  }
  const rows = db.prepare("SELECT channel_jid AS jid, post_url AS url FROM posts ORDER BY channel_jid").all();
  assert.deepEqual(rows.map((row) => ({ ...row })), [
    { jid: "one@newsletter", url: "https://whatsapp.com/channel/first/same" },
    { jid: "two@newsletter", url: "https://whatsapp.com/channel/second/same" },
  ]);
  db.close();
});
