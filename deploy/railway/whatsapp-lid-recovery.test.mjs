import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createRecovery } from './whatsapp-lid-recovery.mjs';
import { patchWhatsAppMonitor, patchWhatsAppIdentity } from './whatsapp-lid-loader.mjs';

const LID = '199630767833213';
const PHONE = '213563719936';
const cfg = { channels: { whatsapp: { accounts: { ads: {}, default: {} } } } };
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lid-recovery-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const warnings = [];
  return { root, warnings, resolve: createRecovery({ root, warn: (s) => warnings.push(s) }) };
}
function pair(root, account, lid = LID, phone = PHONE) {
  const dir = path.join(root, 'credentials', 'whatsapp', account);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, `lid-mapping-${phone}.json`), JSON.stringify(lid));
  fs.writeFileSync(path.join(dir, `lid-mapping-${lid}_reverse.json`), JSON.stringify(phone));
}
function options(extra = {}) { return { cfg, accountId: 'ads', resolveInboundJid: async () => null, ...extra }; }
function msg(extra = {}) { return { key: { remoteJid: `${LID}@lid`, ...extra } }; }

test('known mapping on another account repairs a freshly linked ads account', async (t) => {
  const f = fixture(t); pair(f.root, 'default');
  assert.equal(await f.resolve(msg(), options()), `+${PHONE}`);
});
test('archive survives deletion of credential directories and a process restart', async (t) => {
  const f = fixture(t); pair(f.root, 'ads');
  assert.equal(await f.resolve(msg(), options()), `+${PHONE}`);
  fs.rmSync(path.join(f.root, 'credentials'), { recursive: true });
  const afterRestart = createRecovery({ root: f.root });
  assert.equal(await afterRestart(msg(), options()), `+${PHONE}`);
  assert.equal(fs.statSync(path.join(f.root, 'runtime/whatsapp-identities/mappings.json')).mode & 0o777, 0o600);
});
test('authenticated alternate phone learns a first contact and persists it', async (t) => {
  const f = fixture(t);
  assert.equal(await f.resolve(msg({ remoteJidAlt: `${PHONE}@s.whatsapp.net` }), options()), `+${PHONE}`);
  const afterRestart = createRecovery({ root: f.root });
  assert.equal(await afterRestart(msg(), options()), `+${PHONE}`);
});
test('unknown LID is addressable, never dropped or converted into a phone', async (t) => {
  const f = fixture(t);
  assert.equal(await f.resolve(msg(), options()), `${LID}@lid`);
  assert.equal(await f.resolve(msg(), options()), `${LID}@lid`);
  assert.equal(f.warnings.length, 1);
});
test('hosted LID and device suffix stay a canonical opaque direct identity', async (t) => {
  const f = fixture(t);
  assert.equal(await f.resolve(msg({ remoteJid: `${LID}:3@hosted.lid` }), options()), `${LID}@hosted.lid`);
});
test('arbitrary message text and non-phone alternate cannot assert an identity', async (t) => {
  const f = fixture(t);
  const message = { ...msg({ remoteJidAlt: '263785632943@g.us' }), message: { conversation: `I am +${PHONE}` } };
  assert.equal(await f.resolve(message, options()), `${LID}@lid`);
});
test('one-way mappings are not shared between accounts', async (t) => {
  const f = fixture(t); pair(f.root, 'default');
  fs.unlinkSync(path.join(f.root, 'credentials/whatsapp/default', `lid-mapping-${PHONE}.json`));
  assert.equal(await f.resolve(msg(), options()), `${LID}@lid`);
});
test('conflicting mappings remain opaque, including after restarting', async (t) => {
  const f = fixture(t); pair(f.root, 'default'); pair(f.root, 'ads', LID, '263700000001');
  assert.equal(await f.resolve(msg(), options({ resolveInboundJid: async () => `+${PHONE}` })), `${LID}@lid`);
  assert.equal(await createRecovery({ root: f.root, warn() {} })(msg(), options()), `${LID}@lid`);
});
test('our own line LID still resolves so the sibling loop guard applies', async (t) => {
  const f = fixture(t);
  const dir = path.join(f.root, 'credentials/whatsapp/default'); fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'creds.json'), JSON.stringify({ me: { id: '263785632943:4@s.whatsapp.net', lid: `${LID}:4@lid` } }));
  assert.equal(await f.resolve(msg(), options()), '+263785632943');
});
test('normal phone messages keep upstream resolution', async (t) => {
  const f = fixture(t);
  assert.equal(await f.resolve(msg({ remoteJid: `${PHONE}@s.whatsapp.net` }), options({ resolveInboundJid: async () => `+${PHONE}` })), `+${PHONE}`);
});
test('patch changes only the normalizer and refuses incompatible vendor code', () => {
  const source = `function normalizeWhatsAppIngressPhone(value) {\n\tconst trimmed = value.trim();\n\tif (!trimmed) return null;\n\treturn normalizeE164(trimmed);\n}\nfunction createWhatsAppInboundMessageNormalizer(options) {
    const normalize = async (msg) => {
      const remoteJid = msg.key.remoteJid, group = false, participantJid = undefined;
      const from = group ? remoteJid : await socketSession.resolveInboundJid(remoteJid);
      if (!from) return null;
      const senderE164 = group ? participantJid ? await socketSession.resolveInboundJid(participantJid) : null : from;
      return { from, senderE164 };
    };
  }`;
  const patched = patchWhatsAppMonitor(source);
  assert.equal(patchWhatsAppMonitor(patched), patched);
  assert.match(patched, /genaieResolveWhatsAppSender\(msg/);
  assert.match(patched, /from\?\.includes\("@"\) \? null : from/);
  assert.throws(() => patchWhatsAppMonitor(source.replace('const from =', 'let from =')), /no longer matches/);
  assert.equal(patchWhatsAppMonitor('unrelated module'), 'unrelated module');
});

test('Node loader executes the patched adapter and preserves opaque ingress identity', async (t) => {
  const { spawnSync } = await import('node:child_process');
  const f = fixture(t);
  const dir = path.join(f.root, 'node_modules/@openclaw/whatsapp/dist/.setup');
  fs.mkdirSync(dir, { recursive: true });
  const adapter = path.join(dir, 'monitor-fixture.mjs');
  fs.writeFileSync(adapter, `
function normalizeE164(value) { return '+' + value.replace(/[^0-9]/g, ''); }
function normalizeWhatsAppIngressPhone(value) {
\tconst trimmed = value.trim();
\tif (!trimmed) return null;
\treturn normalizeE164(trimmed);
}
export function createWhatsAppInboundMessageNormalizer(options) {
  const socketSession = options.socketSession;
  return { normalize: async (msg) => {
    const remoteJid = msg.key.remoteJid, group = false, participantJid = undefined;
    const from = group ? remoteJid : await socketSession.resolveInboundJid(remoteJid);
    if (!from) return null;
    const senderE164 = group ? participantJid ? await socketSession.resolveInboundJid(participantJid) : null : from;
    return { from, senderE164, ingressIdentity: normalizeWhatsAppIngressPhone(from) };
  }};
}`);
  const runner = path.join(f.root, 'run.mjs');
  fs.writeFileSync(runner, `import {createWhatsAppInboundMessageNormalizer} from ${JSON.stringify(adapter)};
const factory = createWhatsAppInboundMessageNormalizer({cfg:{},accountId:'ads',socketSession:{resolveInboundJid:async()=>null}});
console.log(JSON.stringify(await factory.normalize({key:{remoteJid:'199630767833213@lid'}})));`);
  const result = spawnSync(process.execPath, ['--import', new URL('./whatsapp-lid-loader.mjs', import.meta.url).pathname, runner], { encoding: 'utf8', env: { ...process.env, HOME: f.root } });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), { from: `${LID}@lid`, senderE164: null, ingressIdentity: `${LID}@lid` });
});

test('learning a phone later preserves an existing opaque conversation across restart', async (t) => {
  const f = fixture(t);
  assert.equal(await f.resolve(msg(), options()), `${LID}@lid`);
  assert.equal(await f.resolve(msg({ remoteJidAlt: `${PHONE}@s.whatsapp.net` }), options()), `${LID}@lid`);
  const restarted = createRecovery({ root: f.root, warn() {} });
  assert.equal(await restarted(msg(), options()), `${LID}@lid`);
  assert.equal(await restarted(msg(), options({ accountId: 'default' })), `+${PHONE}`);
});


test('opaque admission stays the sender identity even if a phone alias appears later', () => {
  const source = `function getSenderIdentity(msg, authDir) { return resolveComparableIdentity(msg.platform.sender, authDir); }`;
  const patched = patchWhatsAppIdentity(source);
  const factory = new Function('resolveComparableIdentity', patched + '; return getSenderIdentity;');
  const getter = factory(() => ({e164:'+213563719936'}));
  const platform = {sender:{e164:'+213563719936'}, senderName:'Customer'};
  const actual = getter({admission:{conversation:{kind:'direct',id:'199630767833213@lid'}},platform});
  assert.deepEqual(actual, {jid:null,lid:'199630767833213@lid',e164:null,name:'Customer'});
  assert.deepEqual(getter({admission:{conversation:{kind:'direct',id:'+213563719936'}},platform}),{e164:'+213563719936'});
  assert.equal(patchWhatsAppIdentity(patched),patched);
  assert.throws(()=>patchWhatsAppIdentity('unsupported adapter'), /no longer matches/);
});
