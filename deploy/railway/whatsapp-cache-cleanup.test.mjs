import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { startExpiredWhatsAppCacheCleanup } from './whatsapp-cache-cleanup.mjs';
import { patchWhatsAppCacheCleanup } from './whatsapp-lid-loader.mjs';

const upstream = fs.readFileSync(new URL('./fixtures/whatsapp-cache-owner-2026.9.9.js', import.meta.url), 'utf8');

function fixture(t, { patched = true, failFetch = false } = {}) {
  const clock = { now: 1_000_000 };
  t.mock.method(Date, 'now', () => clock.now);
  const timers = [];
  const source = (patched ? patchWhatsAppCacheCleanup(upstream) : upstream)
    .split('\n').filter((line) => !line.startsWith('import ')).join('\n');
  const api = new Function('pruneMapToMaxSize', 'asDateTimestampMs', 'resolveExpiresAtMsFromDurationMs',
    'genaieStartExpiredCacheCleanup', source + '\nreturn { createWhatsAppGroupMetadataCacheOwner, readWhatsAppBaileysCacheEntry };')(
    (map, max) => { while (map.size > max) map.delete(map.keys().next().value); },
    (value) => Number.isSafeInteger(value) && value >= 0 ? value : undefined,
    (duration, { nowMs }) => nowMs + duration,
    (caches) => startExpiredWhatsAppCacheCleanup(caches, {
      now: () => clock.now,
      setIntervalFn: (callback, interval) => {
        const timer = { callback, interval, stopped: false, unref() { this.unreferenced = true; } };
        timers.push(timer);
        return timer;
      },
      clearIntervalFn: (timer) => { timer.stopped = true; },
    }),
  );
  const baileysCache = new Map();
  const reconnectCache = new Map();
  let fetches = 0;
  const listeners = new Map();
  const owner = api.createWhatsAppGroupMetadataCacheOwner({
    baileysCache, reconnectCache,
    sock: {
      groupFetchAllParticipating: async () => ({}),
      groupMetadata: async (id) => {
        fetches++;
        if (failFetch) throw new Error('temporary metadata fetch failure');
        return { id, subject: 'Service group', participants: [{ id: 'member@s.whatsapp.net' }] };
      },
    },
    getCurrentSock: () => null,
    resolveInboundJid: async (id) => id,
    listen: (event, callback) => {
      listeners.set(event, callback);
      return () => listeners.delete(event);
    },
    logVerbose() {}, logHydrationWarning() {},
  });
  t.after(() => owner.close());
  owner.start();
  return { clock, timers, owner, baileysCache, reconnectCache, listeners,
    fetches: () => fetches, read: api.readWhatsAppBaileysCacheEntry };
}

test('the real adapter owner keeps valid metadata and refetches identically after expiry', async (t) => {
  const f = fixture(t);
  const first = await f.owner.get('group@g.us');
  const raw = f.baileysCache.get('group@g.us');
  assert.equal(f.timers[0].interval, 60_000);
  assert.equal(f.timers[0].unreferenced, true);
  f.clock.now = first.expires - 1;
  f.timers[0].callback();
  assert.equal(await f.owner.get('group@g.us'), first, 'fresh summary identity is retained');
  assert.equal(f.baileysCache.get('group@g.us'), raw, 'fresh provider metadata is retained');
  assert.equal(f.fetches(), 1);
  f.clock.now = first.expires;
  f.timers[0].callback();
  assert.equal(f.baileysCache.size, 0, 'expired full participant payload is released without another read');
  assert.equal(f.reconnectCache.size, 0);
  const next = await f.owner.get('group@g.us');
  assert.equal(f.fetches(), 2);
  assert.deepEqual(next.participants, first.participants);
  assert.deepEqual(next.mentionParticipants, first.mentionParticipants);
  assert.equal(next.subject, first.subject);
});

test('stock cache readers already reject the same expired metadata', async (t) => {
  const f = fixture(t, { patched: false });
  const first = await f.owner.get('group@g.us');
  f.clock.now = first.expires;
  assert.equal(f.baileysCache.size, 1, 'stock cache keeps expired data until a later read');
  assert.equal(f.read(f.baileysCache, 'group@g.us'), undefined);
  const next = await f.owner.get('group@g.us');
  assert.equal(f.fetches(), 2);
  assert.deepEqual(next.participants, first.participants);
});

test('fresh reconnect fallback survives cleanup and expires under the original policy', async (t) => {
  const f = fixture(t, { failFetch: true });
  const fallback = { subject: 'Reconnect fallback', expires: f.clock.now + 300_000 };
  f.reconnectCache.set('group@g.us', fallback);
  f.timers[0].callback();
  assert.equal(await f.owner.get('group@g.us'), fallback);
  f.clock.now = fallback.expires;
  f.timers[0].callback();
  const expired = await f.owner.get('group@g.us');
  assert.equal(expired.subject, undefined, 'failed fetch never resurrects an expired fallback');
});

test('closing the actual owner detaches listeners, stops cleanup, and releases no valid entries', async (t) => {
  const f = fixture(t);
  const first = await f.owner.get('group@g.us');
  f.owner.start();
  assert.equal(f.timers.length, 1, 'repeated start does not create duplicate timers');
  f.owner.close();
  assert.equal(f.listeners.size, 0);
  assert.equal(f.timers[0].stopped, true);
  f.clock.now = first.expires;
  f.timers[0].callback();
  assert.equal(f.baileysCache.size, 1, 'a queued callback cannot act after owner shutdown');
});

test('cleanup leaves unknown expiries and other account caches alone', () => {
  const own = new Map([['expired', { expires: 1 }], ['fresh', { expires: 3 }], ['unknown', {}]]);
  const other = new Map([['expired', { expires: 1 }]]);
  let callback;
  const stop = startExpiredWhatsAppCacheCleanup([[own, 'expires'], [undefined, 'expiresAt']], {
    now: () => 2, setIntervalFn: (fn) => { callback = fn; return {}; }, clearIntervalFn() {},
  });
  assert.deepEqual([...own.keys()], ['fresh', 'unknown']);
  assert.equal(other.size, 1);
  callback();
  stop();
  stop();
});

test('load-time patch is idempotent and refuses changed adapter custody or expiry contracts', () => {
  const patched = patchWhatsAppCacheCleanup(upstream);
  assert.equal(patchWhatsAppCacheCleanup(patched), patched);
  assert.throws(() => patchWhatsAppCacheCleanup(upstream.replace('expires <= now', 'expires < now')), /contract changed/);
  assert.throws(() => patchWhatsAppCacheCleanup(upstream.replace('started = true;', 'started = false;')), /owner start changed/);
});
