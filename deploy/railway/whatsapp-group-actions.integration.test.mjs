// Compare the group-action integration against the actual pinned adapter,
// offline. The compatibility job downloads the package before entering its
// networkless VM.
//
// The whole feature rests on one assumption: that the socket the monitor hands
// us can leave a group. The adapter never calls `groupLeave` itself -- it comes
// from Baileys, which the adapter pins -- so there is nothing in genaebot that
// would notice the method going away. This is that notice.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { patchWhatsAppGroupActions, patchWhatsAppMonitor } from './whatsapp-lid-loader.mjs';

const root = process.env.GENAIE_WHATSAPP_ROOT;
if (process.env.GENAIE_REQUIRE_INTEGRATION === '1' && !root) throw new Error('Pinned WhatsApp adapter is required for group action compatibility');

test('the pinned adapter still hands out a socket that can leave a group', { skip: !root }, () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json')));
  assert.equal(manifest.version, '2026.9.9');

  // Baileys is where groupLeave comes from, so the version that defines it is
  // the adapter's dependency, not anything this repo installs.
  const baileys = manifest.dependencies?.baileys;
  assert.ok(baileys, 'adapter still depends on baileys');
  const types = process.env.GENAIE_BAILEYS_ROOT;
  if (types) {
    const declaration = fs.readFileSync(path.join(types, 'lib/Socket/groups.d.ts'), 'utf8');
    assert.match(declaration, /groupLeave:\s*\(id: string\) => Promise<void>/,
      `baileys ${baileys} changed the groupLeave contract`);
  }
});

test('the group action helper is injected into the real monitor, not just the fixture', { skip: !root }, () => {
  const directory = path.join(root, 'dist/.setup');
  const monitors = fs.readdirSync(directory).filter(name => name.endsWith('.mjs'))
    .map(name => fs.readFileSync(path.join(directory, name), 'utf8'))
    .filter(source => source.includes('async function monitorWebChannel('));
  assert.equal(monitors.length, 1, 'review adapter module layout on upgrade');

  const patched = patchWhatsAppGroupActions(patchWhatsAppMonitor(monitors[0]));
  assert.match(patched, /genaieCreateWhatsAppGroupActions/);
  // Started with the live socket, and stopped on both the close and the stop
  // path -- a leave must never run against a socket the monitor has replaced.
  assert.equal((patched.match(/genaieGroupActions\.start\(controller\.getCurrentSock\(\)\)/g) || []).length, 1);
  assert.equal((patched.match(/genaieGroupActions\.stop\(\)/g) || []).length, 2);
});
