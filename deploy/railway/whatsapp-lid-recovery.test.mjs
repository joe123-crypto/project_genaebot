import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createRecovery } from './whatsapp-lid-recovery.mjs';
import { patchWhatsAppMonitor, patchWhatsAppIdentity, patchWhatsAppTargets, SUPPORTED_WHATSAPP_VERSION } from './whatsapp-lid-loader.mjs';

const LID = '199630767833213';
const PHONE = '213563719936';
const cfg = { channels: { whatsapp: { dmPolicy: 'open', accounts: { ads: {}, default: {} } } } };
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

test('customer mappings from another account are not identity evidence', async (t) => {
  const f = fixture(t); pair(f.root, 'default');
  assert.equal(await f.resolve(msg(), options()), `${LID}@lid`);
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
test('a one-way file is not local reciprocal mapping evidence', async (t) => {
  const f = fixture(t); pair(f.root, 'ads');
  fs.unlinkSync(path.join(f.root, 'credentials/whatsapp/ads', `lid-mapping-${PHONE}.json`));
  assert.equal(await f.resolve(msg(), options()), `${LID}@lid`);
});
test('conflicting mappings remain opaque, including after restarting', async (t) => {
  const f = fixture(t); pair(f.root, 'ads'); await f.resolve(msg(), options());
  assert.equal(await f.resolve(msg({remoteJidAlt:'263700000001@s.whatsapp.net'}), options()), `${LID}@lid`);
  assert.equal(await createRecovery({ root: f.root, warn() {} })(msg(), options()), `${LID}@lid`);
});
test('our own line LID still resolves so the sibling loop guard applies', async (t) => {
  const f = fixture(t);
  const dir = path.join(f.root, 'credentials/whatsapp/default'); fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'creds.json'), JSON.stringify({ me: { id: '263785632943:4@s.whatsapp.net', lid: `${LID}:4@lid` } }));
  assert.equal(await f.resolve(msg(), options()), '+263785632943');
});
test('normal phone messages retain their canonical address', async (t) => {
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
  assert.throws(() => patchWhatsAppMonitor('renamed upstream normalizer'), /no longer matches/);
});

test('Node loader executes the patched adapter and preserves opaque ingress identity', async (t) => {
  const { spawnSync } = await import('node:child_process');
  const f = fixture(t);
  const dir = path.join(f.root, 'node_modules/@openclaw/whatsapp/dist/.setup');
  fs.mkdirSync(dir, { recursive: true });
  const packageFile = path.join(f.root, 'node_modules/@openclaw/whatsapp/package.json');
  fs.writeFileSync(packageFile, JSON.stringify({version:SUPPORTED_WHATSAPP_VERSION,type:'module'}));
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
  fs.writeFileSync(packageFile, JSON.stringify({version:'2099.1.1',type:'module'}));
  const unsupported = spawnSync(process.execPath, ['--import', new URL('./whatsapp-lid-loader.mjs', import.meta.url).pathname, runner], { encoding:'utf8', env:{...process.env,HOME:f.root} });
  assert.notEqual(unsupported.status,0);
  assert.match(unsupported.stderr,/review the patch before upgrading/);
});

test('learning a phone later preserves an existing opaque conversation across restart', async (t) => {
  const f = fixture(t);
  assert.equal(await f.resolve(msg(), options()), `${LID}@lid`);
  assert.equal(await f.resolve(msg({ remoteJidAlt: `${PHONE}@s.whatsapp.net` }), options()), `${LID}@lid`);
  const restarted = createRecovery({ root: f.root, warn() {} });
  assert.equal(await restarted(msg(), options()), `${LID}@lid`);
  pair(f.root, 'default');
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

test('audit: switching from LID to a phone-addressed message preserves the peer', async (t) => {
  const f = fixture(t);
  assert.equal(await f.resolve(msg(), options()), `${LID}@lid`);
  await f.resolve(msg({remoteJidAlt:`${PHONE}@s.whatsapp.net`}), options());
  assert.equal(await f.resolve(msg({remoteJid:`${PHONE}@s.whatsapp.net`,remoteJidAlt:`${LID}@lid`}), options({resolveInboundJid:async()=>`+${PHONE}`})),`${LID}@lid`);
});
test('audit: malformed archive cannot break every incoming DM', async (t) => {
  const f=fixture(t), file=path.join(f.root,'runtime/whatsapp-identities/mappings.json');
  fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,JSON.stringify({version:1,conflicts:{bad:true},opaque:42,mappings:[]}));
  assert.equal(await f.resolve(msg(),options()),`${LID}@lid`);
});
test('audit: a rejected upstream lookup still admits an opaque DM',async(t)=>{
  const f=fixture(t);
  assert.equal(await f.resolve(msg(),options({resolveInboundJid:async()=>{throw new Error('provider lookup failed');}})),`${LID}@lid`);
});
test('audit: cached aliases alone cannot pass a phone allowlist',async(t)=>{
  const f=fixture(t);pair(f.root,'ads');await f.resolve(msg(),options());
  fs.rmSync(path.join(f.root,'credentials'),{recursive:true});
  const locked={channels:{whatsapp:{dmPolicy:'allowlist',allowFrom:[`+${PHONE}`],accounts:{ads:{}}}}};
  assert.equal(await createRecovery({root:f.root,warn(){}})(msg(),options({cfg:locked})),`${LID}@lid`);
});
test('audit: hosted and normal LID numeric values are not interchangeable',async(t)=>{
  const f=fixture(t);pair(f.root,'ads');
  await f.resolve(msg(),options());
  assert.equal(await f.resolve(msg({remoteJid:`${LID}@hosted.lid`}),options()),`${LID}@hosted.lid`);
});

test('audit: a stalled upstream lookup has a bounded fallback',async(t)=>{
  const f=fixture(t), resolve=createRecovery({root:f.root,warn(){},lookupTimeoutMs:15});
  assert.equal(await resolve(msg(),options({resolveInboundJid:()=>new Promise(()=>{})})),`${LID}@lid`);
});
test('audit: an archived administrator phone is not current authentication',async(t)=>{
  const f=fixture(t);pair(f.root,'ads');await f.resolve(msg(),options());
  fs.rmSync(path.join(f.root,'credentials'),{recursive:true});
  const adminCfg={...cfg,plugins:{entries:{'public-service-gate':{config:{whatsappAdminUsers:[`+${PHONE}`]}}}}};
  assert.equal(await createRecovery({root:f.root,warn(){}})(msg(),options({cfg:adminCfg})),`${LID}@lid`);
});
test('audit: a phone-addressed message without an alternate retains its known opaque peer',async(t)=>{
  const f=fixture(t);await f.resolve(msg(),options());await f.resolve(msg({remoteJidAlt:`${PHONE}@s.whatsapp.net`}),options());
  const again=createRecovery({root:f.root,warn(){}});
  assert.equal(await again(msg({remoteJid:`${PHONE}@s.whatsapp.net`}),options()),`${LID}@lid`);
});


test('audit: target formatting cannot turn a LID into an invented phone or command owner',()=>{
  const source=`function normalizeWhatsAppTarget(value) {\n\tconst candidate = stripWhatsAppTargetPrefixes(value);
return '+'+candidate.replace(/[^0-9]/g,'');}`;
  const patched=patchWhatsAppTargets(source);
  const formatter=new Function('stripWhatsAppTargetPrefixes',patched+';return normalizeWhatsAppTarget;')(value=>value.replace(/^whatsapp:/,''));
  assert.equal(formatter('whatsapp:213563719936@lid'),'213563719936@lid');
  assert.notEqual(formatter('213563719936@lid'),formatter('+213563719936'));
  assert.equal(formatter('123456789:4@hosted.lid'),'123456789@hosted.lid');
  assert.equal(formatter('+213563719936'),'+213563719936');
  assert.equal(patchWhatsAppTargets(patched),patched);
  assert.throws(()=>patchWhatsAppTargets('unsupported upstream'),/no longer matches/);
});

test('audit: command owners and formatted admin numbers cannot be authenticated from cache',async(t)=>{
  for(const extra of [
    {commands:{ownerAllowFrom:[`whatsapp:+${PHONE}`]}},
    {plugins:{entries:{'public-service-gate':{config:{whatsappAdminUsers:['+213 563-719936']}}}}},
  ]){
    const f=fixture(t);pair(f.root,'ads');await f.resolve(msg(),options());
    fs.rmSync(path.join(f.root,'credentials'),{recursive:true});
    assert.equal(await createRecovery({root:f.root,warn(){}})(msg(),options({cfg:{...cfg,...extra}})),`${LID}@lid`);
  }
});


test('adapter version guard tracks the actual image pin',()=>{
  const docker=fs.readFileSync(new URL('./Dockerfile',import.meta.url),'utf8');
  assert.equal(/npm install -g openclaw@([^\s]+)/.exec(docker)?.[1],SUPPORTED_WHATSAPP_VERSION);
});
