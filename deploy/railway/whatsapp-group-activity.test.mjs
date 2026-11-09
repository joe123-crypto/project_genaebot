import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';
import { createWhatsAppGroupActivity, RETENTION_MS } from './whatsapp-group-activity.mjs';
import { createWhatsAppGroupDiscovery } from './whatsapp-group-discovery.mjs';
import { patchWhatsAppGroupActivity, patchWhatsAppGroupDiscovery, patchWhatsAppHealthAudit } from './whatsapp-lid-loader.mjs';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'group-activity-'));
  let stamp = Date.parse('2026-11-09T10:00:00Z');
  const warnings = [], collectors = [];
  const create = (accountId = 'ads', extra = {}) => {
    const collector = createWhatsAppGroupActivity({ root, accountId, now: () => stamp,
      warn: value => warnings.push(value), setIntervalFn: () => ({ unref() {} }), clearIntervalFn: () => {}, ...extra });
    collectors.push(collector);
    const socket = { ev: new EventEmitter(), user: { id: accountId === 'ads' ? '100:2@s.whatsapp.net' : '200@s.whatsapp.net', lid: accountId === 'ads' ? '110@lid' : '220@lid' } };
    collector.start(socket);
    return { collector, socket, send: (messages, type = 'notify') => socket.ev.emit('messages.upsert', { type, messages }) };
  };
  const query = (sql, ...args) => {
    const db = new DatabaseSync(path.join(root, 'runtime/whatsapp-activity.sqlite'));
    try { return db.prepare(sql).all(...args).map(row => ({ ...row })); } finally { db.close(); }
  };
  const message = (id, content = { conversation: 'PRIVATE CONTENT' }, extra = {}) => ({
    key: { remoteJid: '1@g.us', id, participant: '300@s.whatsapp.net', ...extra.key },
    messageTimestamp: stamp / 1000, message: content, ...Object.fromEntries(Object.entries(extra).filter(([key]) => key !== 'key')),
  });
  t.after(() => { for (const c of collectors) c.stop(); fs.rmSync(root, { recursive: true, force: true }); });
  return { root, create, message, query, warnings, now: () => stamp, tick: ms => { stamp += ms; } };
}

test('counts original text and media before admission, deduplicating replays and copies on both lines', t => {
  const f = fixture(t), a = f.create(), b = f.create('default');
  const messages = [f.message('text'), f.message('image', { imageMessage: { caption: 'PRIVATE' } }),
    f.message('wrapped', { ephemeralMessage: { message: { audioMessage: {} } } }),
    f.message('hosted', undefined, { key: { participant: '301:2@hosted' } })];
  a.send(messages); a.send(messages); b.send(messages);
  assert.equal(f.query('SELECT COUNT(*) AS total FROM events')[0].total, 4);
  const data = JSON.stringify(f.query('SELECT * FROM events'));
  assert.doesNotMatch(data, /PRIVATE|300@s|caption|conversation/);
  assert.equal(fs.statSync(path.join(f.root, 'runtime/whatsapp-activity.sqlite')).mode & 0o777, 0o600);
});

test('the real metadata scanner establishes coverage for quiet groups without saving member identities', async t => {
  const f = fixture(t); f.create();
  const discovery = createWhatsAppGroupDiscovery({ root: f.root, accountId: 'ads', now: () => new Date(f.now()) });
  t.after(() => discovery.stop());
  discovery.start({ groupFetchAllParticipating: async () => ({ '1@g.us': { participants: ['PRIVATE MEMBER'] }, '2@g.us': {} }) });
  await discovery.scan();
  assert.deepEqual(f.query('SELECT jid FROM coverage ORDER BY jid'), [{ jid: '1@g.us' }, { jid: '2@g.us' }]);
  assert.equal(f.query('SELECT COUNT(*) AS total FROM events')[0].total, 0);
  const inventory = fs.readFileSync(path.join(f.root, 'runtime/whatsapp-groups/ads.json'), 'utf8');
  assert.doesNotMatch(inventory, /PRIVATE MEMBER|participants/);
});

test('excludes own posts, sibling line aliases, system events, edits, reactions and history sync', t => {
  const f = fixture(t), a = f.create(); f.create('default');
  const ignored = [f.message('own', undefined, { key: { fromMe: true } }),
    f.message('sibling', undefined, { key: { participant: '200:4@s.whatsapp.net' } }),
    f.message('lid', undefined, { key: { participant: '220@hosted.lid' } }),
    f.message('alt', undefined, { key: { participant: '999@lid', participantAlt: '200@s.whatsapp.net' } }),
    f.message('reaction', { reactionMessage: {} }), f.message('edit', { protocolMessage: { editedMessage: { conversation: 'edit' } } }),
    f.message('stub', undefined, { messageStubType: 27 }),
    f.message('dm', undefined, { key: { remoteJid: '300@s.whatsapp.net' } }),
    f.message('receipt', { senderKeyDistributionMessage: {} }),
    f.message('future', undefined, { messageTimestamp: (f.now() + 1000) / 1000 }),
    f.message('old', undefined, { messageTimestamp: (f.now() - RETENTION_MS) / 1000 })];
  a.send(ignored); a.send([f.message('history')], 'append');
  assert.equal(f.query('SELECT COUNT(*) AS total FROM events')[0].total, 0);
});

test('later sibling identity discovery and fromMe echoes remove already observed bot posts', t => {
  const f = fixture(t), a = f.create();
  a.send([f.message('sibling', undefined, { key: { participant: '220@lid' } }), f.message('echo')]);
  assert.equal(f.query('SELECT COUNT(*) AS total FROM events')[0].total, 2);
  f.create('default');
  a.send([f.message('echo', undefined, { key: { fromMe: true } })]);
  a.send([f.message('echo')]);
  assert.equal(f.query('SELECT COUNT(*) AS total FROM events')[0].total, 0);
});

test('restart persists deduplication and retention prunes only expired metadata', t => {
  const f = fixture(t), a = f.create();
  const event = f.message('saved'); a.send([event]); a.collector.stop();
  const b = f.create(); b.send([event]);
  assert.equal(f.query('SELECT COUNT(*) AS total FROM events')[0].total, 1);
  f.tick(RETENTION_MS + 1000); b.collector.heartbeat(); b.send([f.message('new')]);
  assert.deepEqual(f.query('SELECT message_id FROM events'), [{ message_id: 'new' }]);
});

test('coverage starts with observed membership, stops on disconnect and never bridges stalls or rejoins', t => {
  const f = fixture(t), a = f.create();
  a.collector.observeGroups({ '1@g.us': {} });
  f.tick(30_000); a.collector.heartbeat();
  f.tick(120_000); a.collector.heartbeat();
  let segments = f.query('SELECT started_at, through_at, live FROM coverage ORDER BY started_at');
  assert.equal(segments.length, 2);
  assert.ok(segments[1].started_at - segments[0].through_at >= 120_000);
  f.tick(30_000); a.collector.observeGroups({});
  // A new incoming message can establish a rejoin before the next hourly scan.
  f.tick(30_000); a.send([f.message('rejoined')]);
  a.collector.observeGroups({ '1@g.us': {} });
  assert.equal(f.query('SELECT COUNT(*) AS total FROM coverage')[0].total, 3);
  assert.equal(f.query('SELECT COUNT(*) AS total FROM coverage WHERE live = 1')[0].total, 1);
  f.tick(10_000); a.socket.ev.emit('connection.update', { connection: 'close' });
  assert.equal(a.socket.ev.listenerCount('messages.upsert'), 0);
  segments = f.query('SELECT live FROM coverage');
  assert.ok(segments.every(row => row.live === 0));
});

test('storage failure does not escape the socket callback or create false continuous coverage on recovery', t => {
  const f = fixture(t), a = f.create(); a.collector.observeGroups({ '1@g.us': {} });
  const db = new DatabaseSync(path.join(f.root, 'runtime/whatsapp-activity.sqlite'));
  db.exec('BEGIN IMMEDIATE');
  f.tick(30_000); assert.doesNotThrow(() => a.send([f.message('lost')]));
  db.exec('ROLLBACK'); db.close();
  f.tick(30_000); a.collector.heartbeat(); a.send([f.message('saved')]);
  assert.equal(f.warnings.length, 1);
  assert.doesNotMatch(f.warnings[0], /SQL|PRIVATE/);
  assert.equal(f.query('SELECT COUNT(*) AS total FROM coverage')[0].total, 2);
  assert.deepEqual(f.query('SELECT message_id FROM events'), [{ message_id: 'saved' }]);
});

test('replacement monitor detaches old listeners, while opt-out and unavailable storage leave messaging alone', t => {
  const f = fixture(t), a = f.create(); const b = f.create();
  assert.equal(a.socket.ev.listenerCount('messages.upsert'), 0);
  a.send([f.message('obsolete')]); b.send([f.message('current')]);
  assert.deepEqual(f.query('SELECT message_id FROM events'), [{ message_id: 'current' }]);
  const disabled = f.create('disabled', { environment: { GENAIE_GROUP_ACTIVITY: '0' } });
  assert.equal(disabled.socket.ev.listenerCount('messages.upsert'), 0);
  const blocked = path.join(f.root, 'file'); fs.writeFileSync(blocked, 'file');
  assert.doesNotThrow(() => f.create('blocked', { root: blocked }));
  assert.ok(f.warnings.length);
});

test('load-time collector attaches before the inbox and composes with discovery and health patches', () => {
  const original = fs.readFileSync(new URL('./fixtures/whatsapp-lifecycle-2026.9.9.js', import.meta.url), 'utf8');
  const patched = patchWhatsAppGroupDiscovery(patchWhatsAppGroupActivity(patchWhatsAppHealthAudit(original)));
  assert.equal(patchWhatsAppGroupActivity(patched), patched);
  assert.ok(patched.indexOf('genaieGroupActivity.start(sock)') < patched.indexOf('return await (listenerFactory'));
  assert.throws(() => patchWhatsAppGroupActivity(original.replace('createListener: async', 'changed: async')), /contract changed/);
});
