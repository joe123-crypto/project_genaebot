import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import { createCampaignBridge } from '../../workspace/lib/gateway_campaign.mjs';

test('installed SDK reuses a connection and never replays a lost send acknowledgement',
  { skip: !process.env.GENAIE_OPENCLAW_ROOT, timeout: 10_000 }, async (t) => {
    const root = process.env.GENAIE_OPENCLAW_ROOT;
    const require = createRequire(path.join(root, 'package.json'));
    const { WebSocketServer } = require('ws');
    const { GatewayClient } = await import(pathToFileURL(path.join(root, 'dist/plugin-sdk/gateway-runtime.js')));
    const server = new WebSocketServer({ port: 0, host: '127.0.0.1' });
    await once(server, 'listening');
    const calls = [];
    let connections = 0;
    server.on('connection', (socket) => {
      connections++;
      socket.send(JSON.stringify({ type: 'event', event: 'connect.challenge',
        payload: { nonce: 'fixture-challenge', ts: Date.now() } }));
      socket.on('message', (raw) => {
        const frame = JSON.parse(String(raw));
        if (frame.method === 'connect') {
          socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true,
            payload: { type: 'hello-ok', protocol: 3, server: { version: 'fixture', connId: 'fixture' },
              features: { methods: ['send', 'channels.status'], events: [] }, snapshot: {},
              policy: { maxPayload: 1_000_000, maxBufferedBytes: 1_000_000, tickIntervalMs: 30_000 } } }));
          return;
        }
        calls.push(frame);
        if (frame.params.message === 'lose acknowledgement') {
          socket.close(1011, 'fixture lost acknowledgement');
          return;
        }
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true,
          payload: { messageId: 'confirmed' } }));
      });
    });
    const bridge = createCampaignBridge(GatewayClient, { token: 'fixture-token', port: server.address().port });
    t.after(async () => {
      await bridge.close();
      for (const socket of server.clients) socket.terminate();
      await new Promise((resolve) => server.close(resolve));
    });
    const params = { channel: 'whatsapp', to: '1@g.us', accountId: 'ads',
      message: 'caption', mediaUrl: '/fixture/ad.png', idempotencyKey: 'fixture-1' };
    assert.deepEqual(await bridge.request({ method: 'send', params }), { messageId: 'confirmed' });
    await bridge.request({ method: 'send', params: { ...params, to: '2@g.us', idempotencyKey: 'fixture-2' } });
    assert.equal(connections, 1);
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[0].params, params);
    await assert.rejects(bridge.request({ method: 'send', params: {
      ...params, message: 'lose acknowledgement', idempotencyKey: 'fixture-3' } }));
    const deadline = Date.now() + 5000;
    while (connections < 2 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    assert.ok(connections >= 2, 'observe an actual SDK reconnect before checking replay');
    assert.equal(calls.length, 3, 'reconnection must never replay a send');
  });
