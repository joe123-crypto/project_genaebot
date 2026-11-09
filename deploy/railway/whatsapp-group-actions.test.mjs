import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { classifyLeaveError, createWhatsAppGroupActions } from './whatsapp-group-actions.mjs';
import { patchWhatsAppGroupActions, patchWhatsAppGroupDiscovery, patchWhatsAppHealthAudit } from './whatsapp-lid-loader.mjs';

const upstream = fs.readFileSync(new URL('./fixtures/whatsapp-lifecycle-2026.9.9.js', import.meta.url), 'utf8');

function fixture(t, extra = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'group-actions-'));
  const warnings = [], timers = [];
  let stamp = new Date('2026-11-09T06:00:00Z');
  const create = (options = {}) => {
    const actions = createWhatsAppGroupActions({ root, accountId: 'ads', now: () => stamp,
      warn: value => warnings.push(value),
      setIntervalFn: (callback, ms) => { const timer = { callback, ms }; timers.push(timer); return timer; },
      clearIntervalFn: timer => { timer.stopped = true; }, ...extra, ...options });
    t.after(() => actions.stop());
    return actions;
  };
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const queue = (account = 'ads') => path.join(root, 'runtime/whatsapp-group-actions', account);
  const request = (jid, id = crypto.randomUUID(), account = 'ads') => {
    fs.mkdirSync(path.join(queue(account), 'requests'), { recursive: true });
    fs.mkdirSync(path.join(queue(account), 'results'), { recursive: true });
    fs.writeFileSync(path.join(queue(account), 'requests', `${id}.json`),
      JSON.stringify({ id, jid, requestedAt: stamp.toISOString() }));
    return id;
  };
  const result = (id, account = 'ads') => {
    const file = path.join(queue(account), 'results', `${id}.json`);
    return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : null;
  };
  const inventory = (value, account = 'ads') => {
    const file = path.join(root, 'runtime/whatsapp-groups', `${account}.json`);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, JSON.stringify({ version: 1, accountId: account,
      lastAttemptAt: null, lastSuccessAt: null, lastError: null, groups: value }));
    return file;
  };
  const readInventory = (account = 'ads') =>
    JSON.parse(fs.readFileSync(path.join(root, 'runtime/whatsapp-groups', `${account}.json`), 'utf8'));
  return { root, warnings, timers, create, queue, request, result, inventory, readInventory,
    pending: (account = 'ads') => fs.readdirSync(path.join(queue(account), 'requests')) };
}

const JID = '263786627810-1599925488@g.us';
const SEEN = '2026-11-08T06:00:00.000Z';
const socket = (impl = async () => {}) => ({ groupLeave: impl });

test('a queued leave is performed on the account socket and answered', async t => {
  const f = fixture(t), a = f.create();
  const left = [];
  const id = f.request(JID);
  a.start(socket(async jid => { left.push(jid); }));
  await a.poll();

  assert.deepEqual(left, [JID]);
  const answer = f.result(id);
  assert.equal(answer.ok, true);
  assert.equal(answer.already, false);
  assert.equal(answer.error, null);
  assert.equal(answer.jid, JID);
  // The request is consumed, so a reconnect cannot leave the group twice.
  assert.deepEqual(f.pending(), []);
});

test('a successful leave prunes the group from the account inventory', async t => {
  const f = fixture(t), a = f.create();
  f.inventory({ [JID]: { name: 'Bulawayo Markets03', firstSeenAt: SEEN, lastSeenAt: SEEN },
    '120363414718035443@g.us': { name: 'Other', firstSeenAt: SEEN, lastSeenAt: SEEN } });
  f.request(JID);
  a.start(socket());
  await a.poll();

  // Otherwise the group comes straight back under Unassigned, because the
  // inventory still claims a membership that no longer exists.
  assert.deepEqual(Object.keys(f.readInventory().groups), ['120363414718035443@g.us']);
});

test('a group the account is not in is a success, not a failure', async t => {
  for (const status of [403, 404]) {
    const f = fixture(t), a = f.create();
    f.inventory({ [JID]: { name: null, firstSeenAt: SEEN, lastSeenAt: SEEN } });
    const id = f.request(JID);
    a.start(socket(async () => { throw Object.assign(new Error('nope'), { output: { statusCode: status } }); }));
    await a.poll();

    const answer = f.result(id);
    assert.equal(answer.ok, true, `status ${status}`);
    assert.equal(answer.already, true);
    // The end state the request asked for already holds, so the inventory is
    // corrected too.
    assert.deepEqual(f.readInventory().groups, {});
  }
});

test('a real failure is reported in words, never the provider error', async t => {
  const f = fixture(t), a = f.create();
  const id = f.request(JID);
  a.start(socket(async () => { throw new Error('ETIMEDOUT 10.1.2.3 session-key-leak'); }));
  await a.poll();

  const answer = f.result(id);
  assert.equal(answer.ok, false);
  assert.equal(answer.error, 'leave_failed');
  assert.doesNotMatch(JSON.stringify(answer), /ETIMEDOUT|10\.1\.2\.3|session-key/);
});

test('a leave that hangs is answered rather than left pending forever', async t => {
  const f = fixture(t), a = f.create({ timeoutMs: 5 });
  const id = f.request(JID);
  a.start(socket(() => new Promise(() => {})));
  await a.poll();

  assert.equal(f.result(id).error, 'leave_timeout');
  assert.equal(f.result(id).ok, false);
});

test('a request naming something that is not a group is answered, not acted on', async t => {
  const f = fixture(t), a = f.create();
  const calls = [];
  const id = f.request('../../../etc/passwd@g.us');
  a.start(socket(async jid => { calls.push(jid); }));
  await a.poll();

  assert.deepEqual(calls, []);
  assert.equal(f.result(id).error, 'invalid_request');
  assert.deepEqual(f.pending(), []);
});

test('a filename that is not one of ours is never opened', async t => {
  const f = fixture(t), a = f.create();
  f.request(JID, crypto.randomUUID());
  const stray = path.join(f.queue(), 'requests', '..%2f..%2fetc.json');
  fs.writeFileSync(stray, JSON.stringify({ id: 'x', jid: JID }));
  a.start(socket());
  await a.poll();

  // Left exactly where it was: the loop only recognises its own UUID names.
  assert.ok(fs.existsSync(stray));
});

test('a half-written request gets an answer rather than being retried forever', async t => {
  const f = fixture(t), a = f.create();
  const id = crypto.randomUUID();
  fs.mkdirSync(path.join(f.queue(), 'requests'), { recursive: true });
  fs.mkdirSync(path.join(f.queue(), 'results'), { recursive: true });
  fs.writeFileSync(path.join(f.queue(), 'requests', `${id}.json`), '{"id": "');
  a.start(socket());
  await a.poll();

  assert.deepEqual(f.pending(), []);
});

test('the switch keeps it inert', async t => {
  const f = fixture(t), a = f.create({ environment: { GENAIE_GROUP_ACTIONS: '0' } });
  const calls = [];
  const id = f.request(JID);
  a.start(socket(async jid => { calls.push(jid); }));
  await a.poll();

  assert.deepEqual(calls, []);
  assert.equal(f.result(id), null);
  assert.deepEqual(f.timers, []);
});

test('a socket that cannot leave groups says so instead of going quiet', async t => {
  const f = fixture(t), a = f.create();
  a.start({});
  await a.poll();

  assert.match(f.warnings.join('\n'), /cannot leave groups/);
});

test('a stopped instance abandons the queue to its replacement', async t => {
  const f = fixture(t);
  const first = f.create(), second = f.create();
  const calls = [];
  f.request(JID);
  first.start(socket(async jid => { calls.push(`first:${jid}`); }));
  second.start(socket(async jid => { calls.push(`second:${jid}`); }));
  await first.poll();
  await second.poll();

  // The later connection owns the queue; the replaced one does nothing.
  assert.deepEqual(calls, [`second:${JID}`]);
});

test('handling never rejects into the channel lifecycle', async t => {
  const f = fixture(t), a = f.create();
  f.request(JID);
  a.start(socket(async () => { throw new Error('boom'); }));
  await assert.doesNotReject(a.poll());
});

test('abandoned results are swept, collected ones are not waited for', async t => {
  const f = fixture(t), a = f.create({ resultTtlMs: 0 });
  fs.mkdirSync(path.join(f.queue(), 'results'), { recursive: true });
  const stale = path.join(f.queue(), 'results', `${crypto.randomUUID()}.json`);
  fs.writeFileSync(stale, '{}');
  fs.utimesSync(stale, new Date(0), new Date(0));
  a.start(socket());
  await a.poll();

  assert.equal(fs.existsSync(stale), false);
});

test('classifyLeaveError keeps provider status codes out of the answer', () => {
  assert.deepEqual(classifyLeaveError({ leaveTimeout: true }), { ok: false, error: 'leave_timeout' });
  assert.deepEqual(classifyLeaveError({ output: { statusCode: 403 } }), { ok: true, already: true });
  assert.deepEqual(classifyLeaveError({ data: { statusCode: 404 } }), { ok: true, already: true });
  assert.deepEqual(classifyLeaveError({ output: { statusCode: 401 } }), { ok: false, error: 'not_authorized' });
  assert.deepEqual(classifyLeaveError(new Error('x')), { ok: false, error: 'leave_failed' });
});

test('the load-time patch starts and stops it on the pinned adapter, beside the others', () => {
  let patched = patchWhatsAppGroupActions(upstream);
  assert.match(patched, /genaieCreateWhatsAppGroupActions/);
  assert.equal((patched.match(/genaieGroupActions\.start\(controller\.getCurrentSock\(\)\)/g) || []).length, 1);
  assert.equal((patched.match(/genaieGroupActions\.stop\(\)/g) || []).length, 2);
  // Applying it twice must not inject twice.
  assert.equal(patchWhatsAppGroupActions(patched), patched);
  // And it must compose with the patches that already share these anchors.
  const all = patchWhatsAppGroupActions(patchWhatsAppGroupDiscovery(patchWhatsAppHealthAudit(upstream)));
  assert.equal((all.match(/genaieGroupActions\.start\(/g) || []).length, 1);
  assert.equal((all.match(/genaieGroupDiscovery\.start\(/g) || []).length, 1);
});

test('the patch refuses an adapter whose anchors have moved', () => {
  assert.throws(() => patchWhatsAppGroupActions(upstream.replace('statusController.noteConnected();', '')),
    /group actions contract changed/);
});
