import assert from 'node:assert/strict';
import test from 'node:test';
import { createCampaignBridge } from './gateway_campaign.mjs';

function fixture() {
  const instances = [];
  class Client {
    calls = [];
    constructor(options) { this.options = options; instances.push(this); }
    start() { this.options.onHelloOk({}); }
    async request(method, params) {
      this.calls.push({ method, params });
      if (params.fail) throw new Error('acknowledgement lost');
      return { messageId: 'confirmed' };
    }
    async stopAndWait() { this.closed = true; }
  }
  return { instances, bridge: createCampaignBridge(Client, { token: 'test-token' }) };
}

test('campaign reuses one authenticated client and preserves account/media/key', async () => {
  const { bridge, instances } = fixture();
  const params = { channel: 'whatsapp', accountId: 'ads', to: '1@g.us',
    message: 'caption', mediaUrl: '/image.png', idempotencyKey: 'one' };
  await bridge.request({ method: 'send', params });
  await bridge.request({ method: 'send', params: { ...params, to: '2@g.us', idempotencyKey: 'two' } });
  assert.equal(instances.length, 1);
  assert.equal(instances[0].calls.length, 2);
  assert.deepEqual(instances[0].calls[0], { method: 'send', params });
  assert.equal(instances[0].options.url, 'ws://127.0.0.1:18789');
  assert.equal(instances[0].options.sharedStateMode, 'read-only');
  await bridge.close();
  assert.equal(instances[0].closed, true);
});

test('lost acknowledgement is returned without resending', async () => {
  const { bridge, instances } = fixture();
  await assert.rejects(bridge.request({ method: 'send', params: { fail: true } }), /lost/);
  assert.equal(instances[0].calls.length, 1);
  await bridge.close();
});

test('unsupported methods never start a client', async () => {
  const { bridge, instances } = fixture();
  await assert.rejects(bridge.request({ method: 'exec', params: {} }), /unsupported/);
  assert.equal(instances.length, 0);
  await bridge.close();
});

test('authentication failure never issues a send', async () => {
  let requests = 0;
  class Rejected {
    constructor(options) { this.options = options; }
    start() { this.options.onConnectError(new Error('invalid credentials')); }
    request() { requests++; }
    async stopAndWait() {}
  }
  const bridge = createCampaignBridge(Rejected, { token: 'test-token' });
  await assert.rejects(bridge.request({ method: 'send', params: {} }), /credentials/);
  assert.equal(requests, 0);
  await bridge.close();
});

test('a future request succeeds after an initial connection failure recovers', async () => {
  let instance;
  let sends = 0;
  class Recovering {
    constructor(options) { this.options = options; instance = this; }
    start() { this.options.onConnectError(new Error('temporary connection failure')); }
    async request() { sends++; return { messageId: 'new-request' }; }
    async stopAndWait() {}
  }
  const bridge = createCampaignBridge(Recovering, { token: 'fixture' });
  await assert.rejects(bridge.request({ method: 'send', params: {} }), /temporary/);
  const future = bridge.request({ method: 'send', params: {} });
  instance.options.onHelloOk({});
  assert.deepEqual(await future, { messageId: 'new-request' });
  assert.equal(sends, 1, 'failed request must never be replayed');
  await bridge.close();
  instance.options.onHelloOk({});
  await assert.rejects(bridge.request({ method: 'send', params: {} }), /closed/);
});

test('a connection timeout does not poison a later successful hello', async () => {
  let instance;
  class Slow {
    constructor(options) { this.options = options; instance = this; }
    start() {}
    async request() { return { messageId: 'after-timeout' }; }
    async stopAndWait() {}
  }
  const bridge = createCampaignBridge(Slow, { token: 'fixture', connectTimeoutMs: 10 });
  await assert.rejects(bridge.request({ method: 'send', params: {} }), /timed out/);
  instance.options.onHelloOk({});
  assert.equal((await bridge.request({ method: 'send', params: {} })).messageId, 'after-timeout');
  await bridge.close();
});
