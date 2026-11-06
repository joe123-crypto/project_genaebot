import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { readFile, readdir } from 'node:fs/promises';
import test from 'node:test';
import { loadCampaignGatewayClient } from '../../workspace/lib/gateway_campaign.mjs';
import { createAccountController, createControlTransport } from '../../workspace/lib/console_accounts.mjs';

if (process.env.GENAIE_REQUIRE_INTEGRATION === '1' && !process.env.GENAIE_OPENCLAW_ROOT) throw new Error('Pinned runtime is required.');

test('pinned SDK and schemas accept the full account lifecycle', { skip: !process.env.GENAIE_OPENCLAW_ROOT, timeout: 20_000 }, async (t) => {
  const root = process.env.GENAIE_OPENCLAW_ROOT;
  const require = createRequire(path.join(root, 'package.json'));
  const { WebSocketServer } = require('ws');
  const dist = path.join(root, 'dist');
  const protocolFile = (await readdir(dist)).find((file) => file.startsWith('src-') && file.endsWith('.mjs') &&
    file === 'src-D2gvuSyP.mjs');
  assert.ok(protocolFile, 'review the pinned protocol exports before an OpenClaw upgrade');
  const protocolModule = await import(pathToFileURL(path.join(dist, protocolFile)));
  const protocolSource = await readFile(path.join(dist, protocolFile), 'utf8');
  const protocol = Object.fromEntries([...protocolSource.matchAll(/(validate\w+) as ([\w$]+)/g)].map(([, name, alias]) => [name, protocolModule[alias]]));
  const client = await loadCampaignGatewayClient(root);
  const sdkSource = await readFile(path.join(dist, 'plugin-sdk/gateway-runtime.js'), 'utf8');
  assert.match(sdkSource, /GatewayClient/);
  const methods = {
    'config.get': protocol.validateConfigGetParams,
    'config.set': protocol.validateConfigSetParams,
    'channels.status': protocol.validateChannelsStatusParams,
    'channels.start': protocol.validateChannelsStartParams,
    'channels.stop': protocol.validateChannelsStopParams,
    'channels.logout': protocol.validateChannelsLogoutParams,
    'web.login.start': protocol.validateWebLoginStartParams,
    'web.login.wait': protocol.validateWebLoginWaitParams,
  };
  for (const [method, validator] of Object.entries(methods)) assert.equal(typeof validator, 'function', method);
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await once(server, 'listening');
  const transport = createControlTransport(client, { token: 'fixture-token', port: server.address().port });
  t.after(async () => {
    await transport.close();
    for (const socket of server.clients) socket.terminate();
    await new Promise((resolve) => server.close(resolve));
  });
  let config = { channels: { whatsapp: { enabled: true, dmPolicy: 'open', accounts: { default: { enabled: true } } } } };
  const rows = { default: { linked: true, running: true, connected: true } };
  const pending = new Set();
  const calls = [];
  let loseLogoutAck = false;
  const validationErrors = [];
  server.on('connection', (socket) => {
    socket.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: { nonce: 'fixture', ts: Date.now() } }));
    socket.on('message', (raw) => {
      const frame = JSON.parse(String(raw));
      const reply = (payload) => socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }));
      if (frame.method === 'connect') {
        assert.deepEqual(frame.params.scopes, ['operator.admin']);
        reply({ type: 'hello-ok', protocol: 3, server: { version: 'fixture', connId: 'fixture' }, features: { methods: Object.keys(methods), events: [] }, snapshot: {},
          policy: { maxPayload: 1_000_000, maxBufferedBytes: 1_000_000, tickIntervalMs: 30_000 } });
        return;
      }
      calls.push(frame.method);
      if (!methods[frame.method]?.(frame.params)) validationErrors.push({ method: frame.method, errors: methods[frame.method]?.errors });
      const id = frame.params.accountId;
      if (frame.method === 'config.get') reply({ valid: true, hash: 'revision', config, parsed: config, configRevisionHash: 'active', appliedConfigHash: 'active' });
      else if (frame.method === 'config.set') {
        assert.equal(frame.params.baseHash, 'revision');
        config = JSON.parse(frame.params.raw);
        for (const id of Object.keys(config.channels.whatsapp.accounts)) rows[id] ??= { linked: false, running: false, connected: false };
        reply({ ok: true });
      } else if (frame.method === 'channels.status') reply({ channelAccounts: { whatsapp: Object.entries(rows).map(([accountId, row]) => ({ accountId, ...row })) } });
      else if (frame.method === 'channels.start') { rows[id].running = rows[id].connected = true; reply({ started: true }); }
      else if (frame.method === 'channels.stop') { rows[id].running = rows[id].connected = false; reply({ stopped: true }); }
      else if (frame.method === 'channels.logout') {
        rows[id].linked = false;
        if (loseLogoutAck) socket.close(1011, 'fixture lost acknowledgement');
        else reply({ cleared: true });
      }
      else if (frame.method === 'web.login.start') { pending.add(id); reply({ qrDataUrl: 'data:image/png;base64,YQ==' }); }
      else if (frame.method === 'web.login.wait') {
        if (pending.delete(id)) { rows[id].linked = true; reply({ connected: true }); }
        else reply({ connected: false, message: 'No active WhatsApp login in progress.' });
      }
    });
  });
  const controller = createAccountController(transport, { readIdentity: async () => ({ linked: null, number: null }) });
  const session = await controller.dispatch({ action: 'link', accountId: 'sales', create: true });
  assert.equal((await controller.dispatch({ action: 'link-status', sessionId: session.id })).state, 'connected');
  await controller.dispatch({ action: 'pause', accountId: 'sales' });
  await controller.dispatch({ action: 'start', accountId: 'sales' });
  await controller.dispatch({ action: 'unlink', accountId: 'sales' });
  assert.deepEqual(validationErrors, []);
  for (const method of Object.keys(methods)) assert.ok(calls.includes(method), `${method} exercised`);
  assert.equal(config.channels.whatsapp.accounts.sales.enabled, false);
  assert.equal(config.channels.whatsapp.accounts.sales.dmPolicy, 'disabled');
  const before = calls.filter((method) => method === 'channels.logout').length;
  loseLogoutAck = true;
  await assert.rejects(transport.request('channels.logout', { channel: 'whatsapp', accountId: 'sales' }), /could not be confirmed/);
  await transport.request('channels.status', { channel: 'whatsapp', probe: false });
  assert.equal(calls.filter((method) => method === 'channels.logout').length, before + 1, 'reconnection never replays an uncertain mutation');
});
