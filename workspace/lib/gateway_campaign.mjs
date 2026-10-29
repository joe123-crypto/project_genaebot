// JSON-lines bridge. Only the existing Gateway SDK handles authentication and
// reconnection. Requests are issued once; a lost acknowledgement is never replayed.
import { createInterface } from 'node:readline';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// Load the same upstream client re-exported by the SDK, without importing the
// SDK's unrelated server, node-execution and plugin surfaces on every send.
// Fail closed when the pinned package or SDK export contract changes.
export async function loadCampaignGatewayClient(root = '/usr/local/lib/node_modules/openclaw') {
  const pkg = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'));
  if (pkg.version !== '2026.9.9') throw new Error('review campaign client loader before upgrading OpenClaw');
  const sdk = await readFile(path.join(root, 'dist/plugin-sdk/gateway-runtime.js'), 'utf8');
  const matches = [...sdk.matchAll(/import \{ (\w+) as GatewayClient \} from "\.\.\/(client-[\w-]+\.mjs)";/g)];
  if (matches.length !== 1 || !/export \{[^}]*\bGatewayClient\b[^}]*\};/.test(sdk)) {
    throw new Error('pinned GatewayClient SDK export changed');
  }
  const [, symbol, filename] = matches[0];
  const GatewayClient = (await import(pathToFileURL(path.join(root, 'dist', filename))))[symbol];
  if (typeof GatewayClient !== 'function' ||
      !['start', 'request', 'stopAndWait'].every((name) => typeof GatewayClient.prototype[name] === 'function')) {
    throw new Error('pinned GatewayClient API changed');
  }
  return GatewayClient;
}

export function createCampaignBridge(GatewayClient, { token, port = '18789', connectTimeoutMs = 20_000 }) {
  let client;
  let ready = false;
  let closed = false;
  let generation = 0;
  const waiters = new Set();
  function settle(error) {
    for (const waiter of waiters) {
      clearTimeout(waiter.timer);
      if (error) waiter.reject(error);
      else waiter.resolve();
    }
    waiters.clear();
  }
  function start() {
    if (client) return;
    const epoch = ++generation;
    const current = () => !closed && epoch === generation;
    try {
      const created = new GatewayClient({
        url: `ws://127.0.0.1:${port || '18789'}`, token,
        clientName: 'gateway-client', clientDisplayName: 'Genaie campaign sender',
        mode: 'backend', role: 'operator', scopes: ['operator.write', 'operator.read'],
        deviceIdentity: null, sharedStateMode: 'read-only',
        onHelloOk: () => { if (current()) { ready = true; settle(); } },
        onConnectError: (error) => { if (current()) { ready = false; settle(error); } },
        onClose: () => { if (current()) { ready = false; settle(new Error('gateway connection closed')); } },
      });
      client = created;
      client.start();
    } catch (error) {
      ready = false;
      generation++;
      const failed = client;
      client = undefined;
      try { failed?.stop?.(); } catch { /* preserve the connection error */ }
      settle(error);
    }
  }
  function connect() {
    if (closed) return Promise.reject(new Error('campaign client is closed'));
    if (ready && client?.connected !== false) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const waiter = { resolve, reject };
      waiter.timer = setTimeout(() => {
        waiters.delete(waiter);
        reject(new Error('gateway connection timed out'));
      }, connectTimeoutMs);
      waiters.add(waiter);
      start();
    });
  }
  return {
    async request(input) {
      if (!['send', 'channels.status'].includes(input.method)) throw new Error('unsupported campaign method');
      await connect();
      return client.request(input.method, input.params, { timeoutMs: 150_000 });
    },
    async close() {
      closed = true;
      ready = false;
      generation++;
      settle(new Error('campaign client is closed'));
      if (client) await client.stopAndWait({ timeoutMs: 2000 });
    },
  };
}

async function main() {
  const token = process.env.GATEWAY_AUTH_TOKEN;
  if (!token) throw new Error('GATEWAY_AUTH_TOKEN is required');
  const GatewayClient = await loadCampaignGatewayClient();
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
