import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { ACCOUNT_ID, createAccountController, ControlError, readAccountIdentity } from './console_accounts.mjs';

function fixture(options = {}) {
  let config = { gateway: { auth: { token: '__OPENCLAW_REDACTED__' } },
    channels: { whatsapp: { enabled: true, dmPolicy: 'open', accounts: {
      default: { enabled: true }, ads: { enabled: true, dmPolicy: 'disabled' },
    } } } };
  let revision = 1;
  const calls = [];
  const pairing = new Set();
  const states = { default: { linked: true, running: true, connected: true }, ads: { linked: true, running: true, connected: true } };
  const gateway = {
    async request(method, params) {
      calls.push({ method, params });
      if (method === 'config.get') return { valid: true, hash: String(revision), parsed: structuredClone(config), config: structuredClone(config), configRevisionHash: 'applied', appliedConfigHash: 'applied' };
      if (method === 'config.set') {
        if (options.conflict) throw new ControlError(409, 'Configuration changed. Refresh status before retrying.');
        assert.equal(params.baseHash, String(revision));
        config = JSON.parse(params.raw); revision++;
        for (const id of Object.keys(config.channels.whatsapp.accounts)) states[id] ??= { linked: false, running: false, connected: false };
        return { ok: true };
      }
      if (method === 'channels.status') return { partial: options.partial, channelAccounts: { whatsapp: Object.entries(states).map(([accountId, row]) => ({ accountId, ...row, self: { e164: '+263123456789' } })) } };
      const row = states[params.accountId];
      if (method === 'channels.stop') { row.running = false; row.connected = false; return { stopped: true }; }
      if (method === 'channels.start') { row.running = config.channels.whatsapp.accounts[params.accountId].enabled; row.connected = row.linked && row.running; return { started: row.running }; }
      if (method === 'channels.logout') { row.linked = false; return { cleared: true }; }
      if (method === 'web.login.start') { pairing.add(params.accountId); return { qrDataUrl: 'data:image/png;base64,YQ==' }; }
      if (method === 'web.login.wait') {
        if (!pairing.has(params.accountId)) return { connected: false, message: 'No active WhatsApp login in progress.' };
        if (options.nativePairing) return { connected: false, message: 'Still waiting for the QR scan.' };
        pairing.delete(params.accountId);
        if (options.waitError) return { connected: false, message: 'WhatsApp login failed: private gateway detail' };
        row.linked = true;
        return { connected: true };
      }
      throw new Error('Unexpected method');
    },
  };
  const controller = createAccountController(gateway, { ...options, readIdentity: async (_config, id) => ({ linked: states[id]?.linked ?? false, number: null }) });
  return { controller, calls, states, gateway, pairing, config: () => config };
}

test('pause stops activity, keeps login and policies, and a new controller can start it', async () => {
  const f = fixture();
  const paused = await f.controller.dispatch({ action: 'pause', accountId: 'ads' });
  assert.equal(paused.accounts.find((a) => a.id === 'ads').enabled, false);
  assert.deepEqual(f.states.ads, { linked: true, running: false, connected: false });
  assert.deepEqual(f.states.default, { linked: true, running: true, connected: true });
  assert.equal(f.config().channels.whatsapp.accounts.ads.dmPolicy, 'disabled');
  assert.equal(f.calls.some((c) => c.method === 'channels.logout'), false);
  const restarted = createAccountController(f.gateway, { readIdentity: async () => ({ linked: true, number: null }) });
  await restarted.dispatch({ action: 'start', accountId: 'ads' });
  assert.equal(f.states.ads.connected, true);
  assert.equal(f.calls.some((c) => c.method === 'web.login.start'), false);
  assert.equal(f.config().gateway.auth.token, '__OPENCLAW_REDACTED__');
});

test('unlink clears login but retains account config and requires Link before Start', async () => {
  const f = fixture();
  const result = await f.controller.dispatch({ action: 'unlink', accountId: 'default' });
  assert.equal(result.accounts.find((a) => a.id === 'default').linked, false);
  assert.equal(f.config().channels.whatsapp.accounts.default.enabled, false);
  await assert.rejects(f.controller.dispatch({ action: 'start', accountId: 'default' }), /Use Link/);
});

test('new account pairing starts with replies disabled and activates after scan', async () => {
  const f = fixture();
  const session = await f.controller.dispatch({ action: 'link', accountId: 'sales', create: true });
  assert.equal(session.state, 'waiting');
  assert.equal(f.config().channels.whatsapp.accounts.sales.enabled, false);
  assert.equal(f.config().channels.whatsapp.accounts.sales.dmPolicy, 'disabled');
  const again = await f.controller.dispatch({ action: 'link', accountId: 'sales' });
  assert.equal(again.id, session.id);
  assert.equal(f.calls.filter((c) => c.method === 'web.login.start').length, 1);
  await assert.rejects(f.controller.dispatch({ action: 'pause', accountId: 'sales' }), /pairing attempt/);
  const linked = await f.controller.dispatch({ action: 'link-status', sessionId: session.id });
  assert.equal(linked.state, 'connected');
  assert.equal(linked.qrDataUrl, null);
  assert.equal(f.states.sales.connected, true);
  assert.equal(f.config().channels.whatsapp.accounts.sales.dmPolicy, 'disabled');
});

test('pairing failure hides QR and never exposes upstream messages', async () => {
  const f = fixture({ waitError: true });
  const session = await f.controller.dispatch({ action: 'link', accountId: 'ads' });
  const failed = await f.controller.dispatch({ action: 'link-status', sessionId: session.id });
  assert.equal(failed.state, 'failed');
  assert.equal(failed.qrDataUrl, null);
  assert.equal(JSON.stringify(failed).includes('private'), false);
});

test('QR expires without restarting the gateway login on every status poll', async () => {
  let now = 0;
  const f = fixture({ now: () => now });
  const session = await f.controller.dispatch({ action: 'link', accountId: 'ads' });
  now = 180_001;
  const expired = await f.controller.dispatch({ action: 'link-status', sessionId: session.id });
  assert.equal(expired.state, 'expired');
  assert.equal(expired.qrDataUrl, null);
  assert.equal(f.calls.filter((c) => c.method === 'web.login.start').length, 1);
});

test('kill switch blocks Start and Link for default without changing configuration', async () => {
  const f = fixture({ killSwitch: true });
  for (const action of ['start', 'link']) await assert.rejects(f.controller.dispatch({ action, accountId: 'default' }), /REPLIES_DISABLED/);
  assert.equal(f.calls.some((c) => c.method === 'config.set'), false);
});

test('configuration conflicts never proceed to runtime stop or retry a write', async () => {
  const f = fixture({ conflict: true });
  await assert.rejects(f.controller.dispatch({ action: 'pause', accountId: 'ads' }), /Configuration changed/);
  assert.equal(f.calls.filter((c) => c.method === 'config.set').length, 1);
  assert.equal(f.calls.some((c) => c.method === 'channels.stop'), false);
});

test('partial status and missing account IDs fail visibly', async () => {
  const f = fixture({ partial: true });
  await assert.rejects(f.controller.dispatch({ action: 'list' }), /incomplete/);
  await assert.rejects(f.controller.dispatch({ action: 'pause', accountId: 'unknown' }), /does not exist/);
  for (const id of ['../default', '__proto__', 'constructor', 'prototype', 'UPPER']) assert.equal(ACCOUNT_ID.test(id), false);
});

test('identity projection reads only presence and phone number, including paused accounts', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'console-identity-'));
  try {
    const config = { channels: { whatsapp: { accounts: { ads: { enabled: false, authDir: directory } } } } };
    assert.deepEqual(await readAccountIdentity(config, 'ads'), { linked: false, number: null });
    await writeFile(path.join(directory, 'creds.json'), JSON.stringify({ me: { id: '263123456789:4@s.whatsapp.net' }, secret: 'never returned' }));
    assert.deepEqual(await readAccountIdentity(config, 'ads'), { linked: true, number: '+263123456789' });
    await writeFile(path.join(directory, 'creds.json'), '{broken');
    assert.deepEqual(await readAccountIdentity(config, 'ads'), { linked: null, number: null });
  } finally { await rm(directory, { recursive: true, force: true }); }
});

// A process restart loses API sessions, but must not overlook a gateway QR socket.
test('native pairing blocks conflicting controls after API session loss', async () => {
  const f = fixture({ nativePairing: true });
  f.pairing.add('ads');
  for (const action of ['pause', 'unlink', 'start']) {
    await assert.rejects(f.controller.dispatch({ action, accountId: 'ads' }), /pairing is still active/);
  }
  assert.equal(f.calls.some((c) => c.method === 'config.set'), false);
});
