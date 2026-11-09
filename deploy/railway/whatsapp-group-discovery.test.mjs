import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { createWhatsAppGroupDiscovery, DISCOVERY_INTERVAL_MS, inventoryPath, pruneGroupFromInventory } from './whatsapp-group-discovery.mjs';
import { patchWhatsAppGroupDiscovery, patchWhatsAppHealthAudit, patchWhatsAppCacheCleanup } from './whatsapp-lid-loader.mjs';

const upstream = fs.readFileSync(new URL('./fixtures/whatsapp-lifecycle-2026.9.9.js', import.meta.url), 'utf8');

function fixture(t, extra = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'group-discovery-'));
  let stamp = new Date('2026-11-08T06:00:00Z');
  const warnings = [], timers = [];
  const create = (options = {}) => {
    const discovery = createWhatsAppGroupDiscovery({ root, accountId: 'ads', now: () => stamp,
      warn: value => warnings.push(value),
      setIntervalFn: (callback, ms) => { const timer = { callback, ms }; timers.push(timer); return timer; },
      clearIntervalFn: timer => { timer.stopped = true; }, ...extra, ...options });
    t.after(() => discovery.stop());
    return discovery;
  };
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const read = (account = 'ads') => JSON.parse(fs.readFileSync(path.join(root, 'runtime/whatsapp-groups', `${account}.json`)));
  return { root, warnings, timers, create, read, tick: () => { stamp = new Date(stamp.getTime() + 3600_000); } };
}

test('initial scan persists all groups, names and timestamps, without participants or a 100-row limit', async t => {
  const f = fixture(t), d = f.create();
  const groups = Object.fromEntries(Array.from({ length: 150 }, (_, i) => [`${i + 1}@g.us`, { subject: ` Group ${i} `, participants: ['private'] }]));
  groups['person@s.whatsapp.net'] = { subject: 'not a group' };
  d.start({ groupFetchAllParticipating: async () => groups });
  await d.scan();
  const saved = f.read();
  assert.equal(Object.keys(saved.groups).length, 150);
  assert.equal(saved.groups['1@g.us'].name, 'Group 0');
  assert.equal(saved.groups['1@g.us'].firstSeenAt, saved.lastSuccessAt);
  assert.equal(saved.groups['1@g.us'].memberCount, 1);
  assert.equal(saved.groups['1@g.us'].memberCountObservedAt, saved.lastSuccessAt);
  assert.doesNotMatch(JSON.stringify(saved), /private|participants|promos/);
  assert.equal(fs.statSync(path.join(f.root, 'runtime/whatsapp-groups/ads.json')).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(path.join(f.root, 'runtime/whatsapp-groups')), ['ads.json']);
  assert.equal(f.timers[0].ms, DISCOVERY_INTERVAL_MS);
});

test('member totals preserve their observation time when a later scan lacks participant information', async t => {
  const f = fixture(t), d = f.create(); let metadata = { subject: 'Group', participants: [1, 2, 3] };
  d.start({ groupFetchAllParticipating: async () => ({ '1@g.us': metadata }) }); await d.scan();
  const first = f.read().groups['1@g.us'].memberCountObservedAt;
  f.tick(); metadata = { subject: 'Renamed' }; await d.scan();
  assert.equal(f.read().groups['1@g.us'].memberCount, 3);
  assert.equal(f.read().groups['1@g.us'].memberCountObservedAt, first);
  f.tick(); metadata = { size: 0 }; await d.scan();
  assert.equal(f.read().groups['1@g.us'].memberCount, 0);
  assert.notEqual(f.read().groups['1@g.us'].memberCountObservedAt, first);
});

test('hourly refresh updates names, preserves first-seen and missing groups; restart retains history', async t => {
  const f = fixture(t); let groups = { '1@g.us': { subject: 'Original' }, '2@g.us': {} };
  const sock = { groupFetchAllParticipating: async () => groups };
  const d = f.create(); d.start(sock); await d.scan();
  const first = f.read().groups['1@g.us'].firstSeenAt;
  f.tick(); groups = { '1@g.us': { subject: 'Renamed' } };
  f.timers[0].callback(); await d.scan();
  assert.equal(f.read().groups['1@g.us'].name, 'Renamed');
  assert.equal(f.read().groups['1@g.us'].firstSeenAt, first);
  assert.equal(f.read().groups['2@g.us'].lastSeenAt, first);
  d.stop(); f.tick(); groups = { '1@g.us': { subject: '' } };
  const restarted = f.create(); restarted.start(sock); await restarted.scan();
  assert.equal(f.read().groups['1@g.us'].name, 'Renamed');
  assert.equal(f.read().groups['1@g.us'].firstSeenAt, first);
  assert.equal(f.read().groups['2@g.us'].name, null);
});

test('separate accounts may observe the same JID without clobbering each other', async t => {
  const f = fixture(t), ads = f.create(), main = f.create({ accountId: 'default' });
  ads.start({ groupFetchAllParticipating: async () => ({ '1@g.us': { subject: 'Ads name' } }) });
  main.start({ groupFetchAllParticipating: async () => ({ '1@g.us': { subject: 'Main name' } }) });
  await Promise.all([ads.scan(), main.scan()]);
  assert.equal(f.read().groups['1@g.us'].name, 'Ads name');
  assert.equal(f.read('default').groups['1@g.us'].name, 'Main name');
});

test('single flight and replacement monitor discard superseded lookup results', async t => {
  const f = fixture(t), old = f.create(); let resolve, calls = 0;
  old.start({ groupFetchAllParticipating: () => { calls++; return new Promise(r => { resolve = r; }); } });
  const pending = old.scan(); old.scan();
  await delay(0); assert.equal(calls, 1);
  const current = f.create();
  current.start({ groupFetchAllParticipating: async () => ({ '2@g.us': { subject: 'New connection' } }) });
  await current.scan(); resolve({ '1@g.us': { subject: 'Obsolete' } }); await pending;
  assert.deepEqual(Object.keys(f.read().groups), ['2@g.us']);
  old.stop(); await current.scan();
  assert.equal(f.read().groups['2@g.us'].name, 'New connection');
});

test('disconnect stops scans and discards an in-flight result; reconnect scans immediately', async t => {
  const f = fixture(t), d = f.create(); let resolve;
  d.start({ groupFetchAllParticipating: () => new Promise(r => { resolve = r; }) });
  const pending = d.scan(); await delay(0); d.stop();
  resolve({ '1@g.us': { subject: 'Disconnected' } }); await pending;
  f.timers[0].callback(); await d.scan();
  assert.equal(fs.existsSync(path.join(f.root, 'runtime/whatsapp-groups')), false);
  assert.equal(f.timers[0].stopped, true);
  d.start({ groupFetchAllParticipating: async () => ({ '2@g.us': { subject: 'Reconnected' } }) });
  await d.scan(); assert.equal(f.read().groups['2@g.us'].name, 'Reconnected');
});

test('lookup failure preserves the last good inventory and records only a sanitized error', async t => {
  const f = fixture(t), d = f.create(); let fail = false;
  d.start({ groupFetchAllParticipating: async () => {
    if (fail) throw new Error('SECRET credentials and transport details');
    return { '1@g.us': { subject: 'Saved' } };
  } });
  await d.scan(); const good = f.read(); f.tick(); fail = true; await d.scan();
  const failed = f.read();
  assert.equal(failed.lastError, 'lookup_failed');
  assert.equal(failed.lastSuccessAt, good.lastSuccessAt);
  assert.deepEqual(failed.groups, good.groups);
  assert.doesNotMatch(JSON.stringify(failed) + JSON.stringify(f.warnings), /SECRET|credentials/);
});

test('timeout is reported, no overlapping request starts, late result cannot replace the failed scan', async t => {
  const f = fixture(t, { timeoutMs: 5 }), d = f.create(); let resolve, calls = 0;
  d.start({ groupFetchAllParticipating: () => { calls++; return new Promise(r => { resolve = r; }); } });
  await Promise.all([d.scan(), delay(15)]);
  assert.equal(f.read().lastError, 'lookup_timeout');
  await d.scan(); assert.equal(calls, 1);
  resolve({ '1@g.us': { subject: 'Late' } }); await delay(0);
  assert.deepEqual(f.read().groups, {});
});

test('invalid metadata, damaged inventory and unwritable storage never reject into the monitor', async t => {
  const f = fixture(t), d = f.create();
  d.start({ groupFetchAllParticipating: async () => [] }); await d.scan();
  assert.equal(f.read().lastError, 'invalid_metadata');
  const file = path.join(f.root, 'runtime/whatsapp-groups/ads.json'); fs.writeFileSync(file, '{damaged');
  d.start({ groupFetchAllParticipating: async () => ({ '1@g.us': {} }) });
  await assert.doesNotReject(d.scan()); assert.equal(fs.readFileSync(file, 'utf8'), '{damaged');
  const blocked = path.join(f.root, 'blocked'); fs.writeFileSync(blocked, 'file');
  const other = f.create({ root: blocked }); other.start({ groupFetchAllParticipating: async () => ({}) });
  await assert.doesNotReject(other.scan()); assert.ok(f.warnings.length >= 2);
});

test('opt-out and invalid account IDs do not query WhatsApp, warn, or create an inventory', async t => {
  const f = fixture(t);
  for (const options of [{ environment: { GENAIE_GROUP_DISCOVERY: '0' } }, { accountId: '../escape' }]) {
    const d = f.create(options);
    d.start({ groupFetchAllParticipating: () => assert.fail('must not query') }); await d.scan();
  }
  assert.equal(fs.existsSync(path.join(f.root, 'runtime/whatsapp-groups')), false);
  assert.deepEqual(f.warnings, []);
});

// A lifecycle anchor break is reported by the loader; a socket whose group
// lookup is gone passes the anchors and would otherwise fail silently.
test('a socket without the group lookup reports discovery as inactive', async t => {
  const f = fixture(t), d = f.create();
  d.start({});
  await d.scan();
  assert.match(f.warnings.join('\n'), /ads: account socket has no group lookup; discovery inactive/);
  assert.equal(f.timers.length, 0);
  assert.equal(fs.existsSync(path.join(f.root, 'runtime/whatsapp-groups')), false);
});

test('Unicode name limits do not split emoji or persist invalid surrogate characters', async t => {
  const f = fixture(t), d = f.create();
  d.start({ groupFetchAllParticipating: async () => ({
    '1@g.us': { subject: '😀'.repeat(513) }, '2@g.us': { subject: '\ud83d' },
  }) });
  await d.scan();
  assert.equal(f.read().groups['1@g.us'].name, '😀'.repeat(512));
  assert.equal(f.read().groups['2@g.us'].name, '\ufffd');
});

test('load-time integration composes with lifecycle audit and guards the installed monitor contract', () => {
  const patched = patchWhatsAppGroupDiscovery(patchWhatsAppHealthAudit(upstream));
  assert.equal(patchWhatsAppGroupDiscovery(patched), patched);
  assert.match(patched, /genaieGroupDiscovery.start\(controller.getCurrentSock\(\)\)/);
  assert.match(patched, /waitForClose\(\).finally\(\(\) => \{\n\s+genaieGroupDiscovery.stop/);
  assert.throws(() => patchWhatsAppGroupDiscovery(upstream.replace('statusController.noteConnected();', 'changed();')), /contract changed/);
  // The existing metadata-owner cleanup patch also remains independent.
  const cache = fs.readFileSync(new URL('./fixtures/whatsapp-cache-owner-2026.9.9.js', import.meta.url), 'utf8');
  assert.match(patchWhatsAppCacheCleanup(cache), /genaieStartExpiredCacheCleanup/);
});

test('pruneGroupFromInventory removes one group and leaves the rest', async t => {
  const f = fixture(t), d = f.create();
  d.start({ groupFetchAllParticipating: async () => ({ '1@g.us': { subject: 'One' }, '2@g.us': { subject: 'Two' } }) });
  await d.scan();

  const file = inventoryPath(f.root, 'ads');
  assert.equal(await pruneGroupFromInventory(file, 'ads', '1@g.us'), true);
  assert.deepEqual(Object.keys(f.read().groups), ['2@g.us']);
  // Already gone is not an error: a leave must not fail because the inventory
  // had never listed the group.
  assert.equal(await pruneGroupFromInventory(file, 'ads', '1@g.us'), false);
});

test('a lookup that began before a leave cannot put the group back', async t => {
  // The failure this guards: scan() only ever merges what the lookup returned
  // and never removes a group that has gone, so a stale observation written
  // after the prune would stay in the inventory for good -- and the group would
  // keep showing as discovered, which is the whole thing leaving exists to stop.
  const f = fixture(t), d = f.create();
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  d.start({ groupFetchAllParticipating: async () => {
    await gate;
    return { '1@g.us': { subject: 'Departed' }, '2@g.us': { subject: 'Still in' } };
  } });

  const scanning = d.scan();                       // lookup starts at 06:00:00
  // The leave happens while that lookup is in flight.
  await pruneGroupFromInventory(inventoryPath(f.root, 'ads'), 'ads', '1@g.us',
    () => Date.parse('2026-11-08T06:00:30Z'));
  release();                                       // lookup resolves, stale
  await scanning;

  assert.deepEqual(Object.keys(f.read().groups), ['2@g.us']);

  // A later, honest scan still adds it back if the account rejoins: the mark
  // refuses observations older than the leave, not the group forever.
  f.tick();
  const rejoined = f.create();
  rejoined.start({ groupFetchAllParticipating: async () => ({ '1@g.us': { subject: 'Rejoined' } }) });
  await rejoined.scan();
  assert.ok('1@g.us' in f.read().groups, 'a genuine later observation is kept');
});

test('a prune is ordered against a scan rather than racing it', async t => {
  const f = fixture(t), d = f.create();
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  d.start({ groupFetchAllParticipating: async () => {
    await gate;
    return { '1@g.us': { subject: 'One' }, '2@g.us': { subject: 'Two' } };
  } });

  const scanning = d.scan();
  const pruning = pruneGroupFromInventory(inventoryPath(f.root, 'ads'), 'ads', '1@g.us');
  release();
  await Promise.all([scanning, pruning]);

  // Whichever ran first, the file is one or the other's whole result -- never
  // a half-applied mixture, and never a lost scan.
  const saved = f.read();
  assert.ok(saved.lastSuccessAt, 'the scan still landed');
  assert.ok(!('1@g.us' in saved.groups) || '2@g.us' in saved.groups);
});

test('a prune never invents an inventory for an account that has none', async t => {
  const f = fixture(t);
  const file = inventoryPath(f.root, 'nobody');
  assert.equal(await pruneGroupFromInventory(file, 'nobody', '1@g.us'), false);
  assert.equal(fs.existsSync(file), false);
});

test('a prune refuses anything that is not a group id', async t => {
  const f = fixture(t);
  await assert.rejects(
    () => pruneGroupFromInventory(inventoryPath(f.root, 'ads'), 'ads', '../../etc@g.us'),
    /Invalid group id/);
});
