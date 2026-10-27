// JSON-lines bridge. Only the existing Gateway SDK handles authentication and
// reconnection. Requests are issued once; a lost acknowledgement is never replayed.
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

export function createCampaignBridge(GatewayClient, { token, port = '18789' }) {
  let client;
  let ready;
  function connect() {
    if (ready) return ready;
    ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('gateway connection timed out')), 20_000);
      client = new GatewayClient({
        url: `ws://127.0.0.1:${port}`, token,
        clientName: 'gateway-client', clientDisplayName: 'Genaie campaign sender',
        mode: 'backend', role: 'operator', scopes: ['operator.write', 'operator.read'],
        deviceIdentity: null, sharedStateMode: 'read-only',
        onHelloOk: () => { clearTimeout(timer); resolve(); },
        onConnectError: (error) => { clearTimeout(timer); reject(error); },
        onClose: () => { clearTimeout(timer); reject(new Error('gateway connection closed')); },
      });
      client.start();
    });
    return ready;
  }
  return {
    async request(input) {
      if (!['send', 'channels.status'].includes(input.method)) throw new Error('unsupported campaign method');
      await connect();
      return client.request(input.method, input.params, { timeoutMs: 150_000 });
    },
    async close() {
      if (client) await client.stopAndWait({ timeoutMs: 2000 });
    },
  };
}

async function main() {
  const token = process.env.GATEWAY_AUTH_TOKEN;
  if (!token) throw new Error('GATEWAY_AUTH_TOKEN is required');
  const { GatewayClient } = await import('/usr/local/lib/node_modules/openclaw/dist/plugin-sdk/gateway-runtime.js');
  const bridge = createCampaignBridge(GatewayClient, { token, port: process.env.OPENCLAW_GATEWAY_PORT });
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  try {
    for await (const line of lines) {
      let input;
      try {
        input = JSON.parse(line);
        const payload = await bridge.request(input);
        process.stdout.write(JSON.stringify({ id: input.id, ok: true, payload }) + '\n');
      } catch (error) {
        process.stdout.write(JSON.stringify({ id: input?.id, ok: false,
          error: { code: error.code, message: String(error.message ?? error), details: error.details } }) + '\n');
      }
    }
  } finally {
    await bridge.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
}
