import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  MAX_ENTRIES,
  noteHumanTakeover,
  resetHumanTakeoverWarning,
  takeoverKey,
  takeoverKeys,
  takeoverStorePath,
} from './whatsapp-human-takeover.mjs';
import { patchWhatsAppHumanTakeover } from './whatsapp-lid-loader.mjs';

const CUSTOMER = '+263771234567';
const SELF = '+263785632943';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'human-takeover-'));
  let stamp = Date.parse('2026-11-14T08:00:00Z');
  const warnings = [];
  resetHumanTakeoverWarning();
  t.after(() => {
    fs.rmSync(root, { recursive: true, force: true });
    resetHumanTakeoverWarning();
  });
  const note = (params = {}) => noteHumanTakeover({
    accountId: 'default',
    from: CUSTOMER,
    remoteJid: '263771234567@s.whatsapp.net',
    selfE164: SELF,
    messageTimestampMs: stamp,
    connectedAtMs: stamp - 600_000,
    root,
    environment: {},
    now: () => stamp,
    warn: (value) => warnings.push(value),
    ...params,
  });
  const read = () => {
    try { return JSON.parse(fs.readFileSync(takeoverStorePath(root), 'utf8')); } catch { return null; }
  };
  return { root, note, read, warnings, tick: (ms) => { stamp += ms; }, at: () => new Date(stamp).toISOString() };
}

test('an operator reply is recorded once, under the identity the gate looks up', (t) => {
  const f = fixture(t);
  assert.deepEqual(f.note(), [CUSTOMER]);
  assert.deepEqual(f.read(), { version: 1, takeovers: { [CUSTOMER]: { at: '2026-11-14T08:00:00.000Z', accountId: 'default', peers: [CUSTOMER] } } });

  // A person answering properly sends several messages; only the first counts,
  // and the timestamp a later "resume" is compared against must not move.
  f.tick(60_000);
  assert.equal(f.note(), null);
  assert.equal(f.read().takeovers[CUSTOMER].at, '2026-11-14T08:00:00.000Z');
});

test('an opaque WhatsApp id is recorded under its own identity, never a phone', (t) => {
  const f = fixture(t);
  assert.deepEqual(f.note({ from: '123456@lid', remoteJid: '123456:7@lid' }), ['123456@lid']);
  assert.deepEqual(Object.keys(f.read().takeovers), ['123456@lid']);
});

test('a raw address that disagrees with the resolved identity is recorded both ways', (t) => {
  const f = fixture(t);
  assert.deepEqual(f.note({ from: CUSTOMER, remoteJid: '987@lid' }), [CUSTOMER, '987@lid']);
});

test('keys match the plugin normalizePhone contract', () => {
  assert.equal(takeoverKey('263771234567@s.whatsapp.net'), '+263771234567');
  assert.equal(takeoverKey('263771234567:12@s.whatsapp.net'), '+263771234567');
  assert.equal(takeoverKey('263771234567:12@hosted'), '+263771234567');
  assert.equal(takeoverKey('123@lid'), '123@lid');
  assert.equal(takeoverKey('123:4@hosted.lid'), '123@hosted.lid');
  assert.equal(takeoverKey(' +263 77 123 4567 '), '+263771234567');
  assert.equal(takeoverKey('1@g.us'), '');
  assert.equal(takeoverKey('status@broadcast'), '');
  assert.equal(takeoverKey(undefined), '');
  assert.deepEqual(takeoverKeys({ from: CUSTOMER, remoteJid: '263771234567@s.whatsapp.net' }), [CUSTOMER]);
  assert.deepEqual(takeoverKeys({}), []);
});

test('nothing a person cannot be handed is recorded', (t) => {
  const f = fixture(t);
  for (const params of [
    { group: true },
    { remoteJid: '1@g.us' },
    { from: '', remoteJid: 'status@broadcast' },
    { from: '', remoteJid: '1@newsletter' },
    // The self-chat is how the operator talks to the bot. Taking it over would
    // silence the operator's own admin commands.
    { from: SELF, remoteJid: '263785632943@s.whatsapp.net' },
    // Nothing to key on.
    { from: '', remoteJid: '' },
  ]) {
    assert.equal(f.note(params), null, JSON.stringify(params));
  }
  assert.equal(f.read(), null);
  assert.deepEqual(f.warnings, []);
});

test('a reconnect replay of the bot\'s own sends is not mistaken for a person', (t) => {
  const f = fixture(t);
  // The adapter drops these as stale appends, but only after inbound access
  // control has run, so they reach this module. Older than the connection
  // means the dedupe cache can no longer vouch for them either way.
  assert.equal(f.note({ messageTimestampMs: 1_000_000, connectedAtMs: 5_000_000 }), null);
  assert.equal(f.read(), null);
  // Inside the grace window it is live traffic, not a replay.
  assert.deepEqual(f.note({ messageTimestampMs: 5_000_000 - 30_000, connectedAtMs: 5_000_000 }), [CUSTOMER]);
});

test('a message with no usable clock is still recorded', (t) => {
  const f = fixture(t);
  assert.deepEqual(f.note({ messageTimestampMs: undefined, connectedAtMs: undefined }), [CUSTOMER]);
});

test('every spelling of the chat is recorded on each entry, so resume can release them all', (t) => {
  const f = fixture(t);
  f.note({ from: CUSTOMER, remoteJid: '987@lid' });
  const stored = f.read().takeovers;
  assert.deepEqual(stored[CUSTOMER].peers, [CUSTOMER, '987@lid']);
  assert.deepEqual(stored['987@lid'].peers, [CUSTOMER, '987@lid']);
});

test('the kill switch stops detection without stopping the channel', (t) => {
  const f = fixture(t);
  assert.equal(f.note({ environment: { GENAIE_HUMAN_TAKEOVER: '0' } }), null);
  assert.equal(f.read(), null);
  // Any other value leaves it on.
  assert.deepEqual(f.note({ environment: { GENAIE_HUMAN_TAKEOVER: '1' } }), [CUSTOMER]);
});

test('a store that cannot be written warns once and lets the bot keep talking', (t) => {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.root, 'runtime'), 'not a directory\n');
  assert.equal(f.note(), null);
  assert.equal(f.note({ from: '+263770000000' }), null);
  assert.equal(f.warnings.length, 1);
  assert.match(f.warnings[0], /^\[whatsapp-takeover\] could not record an operator reply/);
});

test('the store is capped', (t) => {
  const f = fixture(t);
  const takeovers = {};
  for (let index = 0; index < MAX_ENTRIES + 10; index++) {
    takeovers[`+2637${String(index).padStart(8, '0')}`] = { at: '2020-01-01T00:00:00.000Z', accountId: 'default' };
  }
  const filePath = takeoverStorePath(f.root);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify({ version: 1, takeovers }));

  assert.deepEqual(f.note(), [CUSTOMER]);
  const stored = f.read().takeovers;
  assert.equal(Object.keys(stored).length, MAX_ENTRIES);
  assert.ok(stored[CUSTOMER], 'the newest takeover survives the cap');
});

test('a corrupt store is replaced rather than trusted', (t) => {
  const f = fixture(t);
  const filePath = takeoverStorePath(f.root);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, '{ not json');
  assert.deepEqual(f.note(), [CUSTOMER]);
  assert.deepEqual(Object.keys(f.read().takeovers), [CUSTOMER]);
});

const MONITOR = [
  'function normalizeInboundMessage(msg) {',
  '\tif (shouldSkipRecentOutboundEcho(msg)) return null;',
  '\tif (!hasInboundUserContent(msg.message ?? void 0)) return null;',
  '}',
  'async function checkInboundAccessControl(params) {',
  '\tif (!params.group) {',
  '\t\tif (params.isFromMe && (policy.account.selfChatMode === false || !policy.isSamePhone(params.from))) {',
  '\t\t\tlogWhatsAppVerbose$2(params.verbose, "Skipping outbound DM (fromMe); no pairing reply needed.");',
  '\t\t\treturn blockedInboundAccess(policy);',
  '\t\t}',
  '\t}',
  '}',
].join('\n');

test('the adapter patch records the peer before the message is dropped', () => {
  const patched = patchWhatsAppHumanTakeover(MONITOR);
  assert.match(patched, /^\/\/ GENAIE_WHATSAPP_HUMAN_TAKEOVER_V1\nimport \{ noteHumanTakeover as genaieNoteHumanTakeover \} from "[^"]*whatsapp-human-takeover\.mjs";\n/);
  const call = 'genaieNoteHumanTakeover({ accountId: params.accountId, from: params.from, remoteJid: params.remoteJid, group: params.group, selfE164: params.selfE164, messageTimestampMs: params.messageTimestampMs, connectedAtMs: params.connectedAtMs });';
  assert.ok(patched.includes(call));
  assert.ok(patched.indexOf(call) < patched.indexOf('return blockedInboundAccess(policy);'), 'recorded before the drop');
  // The log line must survive: it is the anchor and the operator reads it.
  assert.ok(patched.includes('Skipping outbound DM (fromMe); no pairing reply needed.'));
  assert.equal(patchWhatsAppHumanTakeover(patched), patched);
});

test('the patch refuses an adapter it no longer recognises', () => {
  for (const broken of [
    MONITOR.replace('logWhatsAppVerbose$2(params.verbose, "Skipping outbound DM (fromMe); no pairing reply needed.");', 'logSomethingElse();'),
    MONITOR.replace('if (params.isFromMe && (policy.account.selfChatMode === false || !policy.isSamePhone(params.from))) {', 'if (params.isFromMe) {'),
    MONITOR + '\n' + MONITOR,
  ]) {
    assert.throws(() => patchWhatsAppHumanTakeover(broken), /human takeover contract changed/);
  }
  // Without the echo and content filters in front of it, the branch no longer
  // means "a person typed this".
  for (const ordering of ['\tif (shouldSkipRecentOutboundEcho(msg)) return null;\n', '\tif (!hasInboundUserContent(msg.message ?? void 0)) return null;\n']) {
    assert.throws(() => patchWhatsAppHumanTakeover(MONITOR.replace(ordering, '')), /outbound echo filtering contract changed/);
  }
});

// The detector writes this store and the plugin reads it, but they are deployed
// separately: this file is baked into the image beside the adapter, the plugin
// lives on the volume. Nothing but these two tests keeps them agreeing.
test('the detector and the plugin name the same store', async (t) => {
  const { getTakeoversPath } = await import('../../workspace/plugins/public-service-gate/lib/takeover.js');
  const had = Object.prototype.hasOwnProperty.call(process.env, 'OPENCLAW_STATE_DIR');
  const previous = process.env.OPENCLAW_STATE_DIR;
  delete process.env.OPENCLAW_STATE_DIR;
  t.after(() => { if (had) process.env.OPENCLAW_STATE_DIR = previous; else delete process.env.OPENCLAW_STATE_DIR; });
  const api = { config: { plugins: { entries: { 'public-service-gate': { config: {} } } } }, logger: { debug() {} } };
  assert.equal(takeoverStorePath(), getTakeoversPath(api));
});

test('the detector keys peers exactly as the plugin does', async () => {
  const { normalizePhone } = await import('../../workspace/plugins/public-service-gate/lib/events.js');
  for (const value of ['+263771234567', '263771234567', '263771234567@s.whatsapp.net',
    '263771234567:12@s.whatsapp.net', '263771234567:12@hosted', '4455@lid', '4455:3@hosted.lid',
    '1@g.us', 'status@broadcast', 'nobody@example.com', ' ', '']) {
    assert.equal(takeoverKey(value), normalizePhone(value), JSON.stringify(value));
  }
});
