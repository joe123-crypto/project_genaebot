import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { AD_REF_PREFIXES, extractAdRefs, getAdRefsPath, recordAdRefs } from "./ad-refs.js";

function apiWithStateDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ad-refs-"));
  const warnings = [];
  return {
    dir,
    warnings,
    api: {
      config: {
        plugins: { entries: { "public-service-gate": { config: { serviceStatePath: path.join(dir, "state.json") } } } },
      },
      logger: { info() {}, warn(msg) { warnings.push(msg); }, debug() {} },
    },
  };
}

test("extractAdRefs finds promo refs in the prefilled click-to-chat text", () => {
  assert.deepEqual(extractAdRefs("Hi, I saw your borehole ad (ref BH-7K2)"), ["BH-7K2"]);
  // Case-insensitive, normalised to upper case, de-duplicated.
  assert.deepEqual(extractAdRefs("ref cv-a1b and CV-A1B again"), ["CV-A1B"]);
  assert.deepEqual(extractAdRefs("GN-0Z9 GL-XYZ"), ["GN-0Z9", "GL-XYZ"]);
});

test("extractAdRefs ignores unknown prefixes, ambiguous letters and plain text", () => {
  assert.deepEqual(extractAdRefs("hi"), []);
  assert.deepEqual(extractAdRefs(""), []);
  assert.deepEqual(extractAdRefs(undefined), []);
  assert.deepEqual(extractAdRefs("ref XX-7K2"), []); // not a promo prefix
  assert.deepEqual(extractAdRefs("ref BH-7KO"), []); // O is not in the alphabet
  assert.deepEqual(extractAdRefs("ref BH-7K22"), []); // too long
  assert.deepEqual(extractAdRefs("ABH-7K2"), []); // not on a word boundary
});

test("prefix list matches the promo crons", () => {
  assert.deepEqual([...AD_REF_PREFIXES].sort(), ["BH", "BM", "CV", "FT", "GL", "GN", "PB", "SK"]);
});

test("recordAdRefs counts each ref and distinct senders without storing phone numbers", () => {
  const { api } = apiWithStateDir();
  const t0 = new Date("2026-10-01T08:00:00Z");
  const t1 = new Date("2026-10-02T09:00:00Z");
  assert.deepEqual(recordAdRefs(api, "+263771111111", "Hi, I saw your borehole ad (ref BH-7K2)", t0), ["BH-7K2"]);
  recordAdRefs(api, "+263771111111", "ref BH-7K2", t1);
  recordAdRefs(api, "+263772222222", "ref BH-7K2 and FT-ABC", t1);

  const raw = fs.readFileSync(getAdRefsPath(api), "utf8");
  assert.doesNotMatch(raw, /26377/);
  const { refs } = JSON.parse(raw);
  assert.equal(refs["BH-7K2"].count, 3);
  assert.equal(refs["BH-7K2"].uniqueSenders, 2);
  assert.equal(refs["BH-7K2"].firstSeen, t0.toISOString());
  assert.equal(refs["BH-7K2"].lastSeen, t1.toISOString());
  assert.equal(refs["FT-ABC"].count, 1);
});

test("recordAdRefs is a no-op without a ref and never throws on I/O failure", () => {
  const { api, dir } = apiWithStateDir();
  assert.deepEqual(recordAdRefs(api, "+263771111111", "hello"), []);
  assert.equal(fs.existsSync(getAdRefsPath(api)), false);

  // Make the target path a directory so the rename fails.
  fs.mkdirSync(path.join(dir, "ad-refs.json"));
  const { warnings } = { warnings: [] };
  api.logger.warn = (msg) => warnings.push(msg);
  assert.deepEqual(recordAdRefs(api, "+263771111111", "ref GN-123"), ["GN-123"]);
  assert.equal(warnings.length, 1);
});
