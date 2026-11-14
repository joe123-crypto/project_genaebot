// Compare the integration against the actual pinned adapter, offline. The
// compatibility job downloads the package before entering its networkless VM.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { patchWhatsAppGroupActions, patchWhatsAppGroupActivity, patchWhatsAppGroupDiscovery, patchWhatsAppHealthAudit, patchWhatsAppHumanTakeover, patchWhatsAppCacheCleanup, patchWhatsAppMonitor } from './whatsapp-lid-loader.mjs';

const root = process.env.GENAIE_WHATSAPP_ROOT;
if (process.env.GENAIE_REQUIRE_INTEGRATION === '1' && !root) throw new Error('Pinned WhatsApp adapter is required for discovery compatibility');

test('actual pinned WhatsApp lifecycle and socket contracts accept every composed load-time patch', { skip: !root }, t => {
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'package.json'))).version, '2026.9.9');
  const directory = path.join(root, 'dist/.setup');
  const files = fs.readdirSync(directory).filter(name => name.endsWith('.mjs'));
  const monitors = files.map(name => ({ name, source: fs.readFileSync(path.join(directory, name), 'utf8') }))
    .filter(file => file.source.includes('async function monitorWebChannel('));
  assert.equal(monitors.length, 1, 'review adapter module layout on upgrade');
  const original = monitors[0].source;
  assert.match(original, /getCurrentSock\(\)/, 'adapter exposes the existing account socket');
  assert.match(original, /groupFetchAllParticipating\(\)/, 'adapter uses the unbounded group lookup');
  // Compose in the order whatsapp-lid-loader.mjs applies at load time, so an
  // anchor one patch moves under another is caught here and not in production.
  const patched = patchWhatsAppCacheCleanup(patchWhatsAppHumanTakeover(patchWhatsAppGroupActions(patchWhatsAppGroupDiscovery(patchWhatsAppGroupActivity(patchWhatsAppHealthAudit(patchWhatsAppMonitor(original)))))));
  assert.equal(patchWhatsAppGroupDiscovery(patched), patched);
  assert.equal(patchWhatsAppGroupActivity(patched), patched);
  assert.equal(patchWhatsAppGroupActions(patched), patched);
  assert.equal(patchWhatsAppHumanTakeover(patched), patched);
  // Handover detection rides on three adapter behaviours, in this order: the
  // bot's own sends are dropped as echoes, receipts and reactions are dropped
  // as non-content, and only then is a fromMe DM rejected. Lose any of them
  // and "a person typed this" stops being true.
  assert.ok(original.indexOf('if (shouldSkipRecentOutboundEcho(msg)) return null;')
    < original.indexOf('if (!hasInboundUserContent(msg.message ?? void 0)) return null;'),
    'adapter drops the bot\'s own echoes before filtering for user content');
  assert.ok(original.includes('sendTrackedMessage'), 'every bot send is tracked for echo suppression');
  assert.ok(original.includes("recentOutboundMessages = createDedupeCache({\n\tttlMs: 12e5,"),
    'echo suppression still remembers sends for 20 minutes; CONNECT_GRACE_MS assumes it');
  assert.equal((patched.match(/genaieNoteHumanTakeover\(\{/g) || []).length, 1);
  // The clock the replay guard needs has to reach inbound access control.
  assert.ok(original.includes("\t\t\tmessageTimestampMs,\n\t\t\tconnectedAtMs: socketSession.connectedAtMs,"),
    'inbound access control still receives the message and connection clocks');
  assert.match(original, /messages\.upsert/, 'collector receives raw socket messages before admission');
  // Every helper has to survive the others' insertions at the shared anchors.
  assert.equal((patched.match(/genaieGroupDiscovery\.start\(/g) || []).length, 1);
  assert.equal((patched.match(/genaieGroupActivity\.start\(/g) || []).length, 1);
  assert.equal((patched.match(/genaieGroupActions\.start\(/g) || []).length, 1);
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'discovery-adapter-'));
  t.after(() => fs.rmSync(temporary, { recursive: true, force: true }));
  const filename = path.join(temporary, 'monitor.mjs'); fs.writeFileSync(filename, patched);
  const syntax = spawnSync(process.execPath, ['--check', filename], { encoding: 'utf8' });
  assert.equal(syntax.status, 0, syntax.stderr);
  const fixture = fs.readFileSync(new URL('./fixtures/whatsapp-lifecycle-2026.9.9.js', import.meta.url), 'utf8');
  const start = original.indexOf('async function monitorWebChannel(');
  // The executed lifecycle fixture is the actual vendor function, not a
  // separately authored approximation of its socket ownership or cleanup.
  assert.ok(original.slice(start).startsWith(fixture.slice(fixture.indexOf('async function monitorWebChannel(')).trim()));
});
