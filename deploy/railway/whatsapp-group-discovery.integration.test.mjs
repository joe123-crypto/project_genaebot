// Compare the integration against the actual pinned adapter, offline. The
// compatibility job downloads the package before entering its networkless VM.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { patchWhatsAppGroupActivity, patchWhatsAppGroupDiscovery, patchWhatsAppHealthAudit, patchWhatsAppCacheCleanup, patchWhatsAppMonitor } from './whatsapp-lid-loader.mjs';

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
  const patched = patchWhatsAppCacheCleanup(patchWhatsAppGroupDiscovery(patchWhatsAppGroupActivity(patchWhatsAppHealthAudit(patchWhatsAppMonitor(original)))));
  assert.equal(patchWhatsAppGroupDiscovery(patched), patched);
  assert.equal(patchWhatsAppGroupActivity(patched), patched);
  assert.match(original, /messages\.upsert/, 'collector receives raw socket messages before admission');
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
