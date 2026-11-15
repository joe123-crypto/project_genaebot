import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';

const root = process.env.GENAIE_OPENCLAW_ROOT;
if (process.env.GENAIE_REQUIRE_INTEGRATION === '1' && !root) throw new Error('Pinned OpenClaw is required.');
const token = 'customer-bot-fixture-token-000000';
const gatewayToken = 'fixture-gateway-token';

async function temporaryPort() {
  const server = http.createServer();
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const { port } = server.address();
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function waitFor(child, ready) {
  const deadline = Date.now() + 30_000;
  while (!ready()) {
    if (child.exitCode !== null || Date.now() > deadline) throw new Error(child.logs());
    await delay(200);
  }
}

function start(t, program, args, env) {
  const child = spawn(program, args, { env });
  let output = '';
  child.logs = () => output;
  for (const pipe of [child.stdout, child.stderr]) pipe.on('data', (chunk) => { output = (output + chunk).slice(-25000); });
  t.after(async () => {
    if (child.exitCode !== null || child.signalCode) return;
    child.kill('SIGTERM');
    const exited = await Promise.race([once(child, 'exit').then(() => true), delay(4000, false, { ref: false })]);
    if (!exited) child.kill('SIGKILL');
  });
  return child;
}

test('published content reaches the next model call in the same customer WhatsApp session only',
  { skip: !root, timeout: 120_000 }, async (t) => {
    const { GatewayClient } = await import(pathToFileURL(path.join(root, 'dist/plugin-sdk/gateway-runtime.js')));
    const home = await fs.mkdtemp(path.join(os.tmpdir(), 'customer-bot-test-'));
    const state = path.join(home, 'state');
    const docs = path.join(state, 'workspace', 'docs');
    await fs.mkdir(docs, { recursive: true });
    const source = fileURLToPath(new URL('../../workspace/', import.meta.url));
    const plugin = path.join(state, 'workspace', 'plugins', 'public-service-gate');
    await fs.cp(path.join(source, 'plugins', 'public-service-gate'), plugin, { recursive: true });
    if (process.getuid?.() === 0) {
      // The bind-mounted checkout can belong to another user. OpenClaw checks
      // the ownership of the actual plugin files before loading them.
      await fs.chown(plugin, 0, 0);
      for (const file of await fs.readdir(plugin, { recursive: true })) await fs.chown(path.join(plugin, file), 0, 0);
    }
    for (const name of ['public-whatsapp-reply-instructions.txt', 'business-info.md']) {
      await fs.copyFile(path.join(source, 'docs', name), path.join(docs, name));
    }
    const requests = [];
    const provider = http.createServer(async (incoming, outgoing) => {
      if (incoming.url === '/v1/models') {
        outgoing.writeHead(200, { 'content-type': 'application/json' });
        outgoing.end(JSON.stringify({ data: [{ id: 'customer-fixture', object: 'model' }] }));
        return;
      }
      let body = '';
      for await (const chunk of incoming) body += chunk;
      requests.push(JSON.parse(body));
      outgoing.writeHead(200, { 'content-type': 'text/event-stream' });
      for (const [delta, finish_reason] of [[{ role: 'assistant', content: 'fixture-ok' }, null], [{}, 'stop']]) {
        outgoing.write('data: ' + JSON.stringify({ id: 'fixture', object: 'chat.completion.chunk', created: 1,
          model: 'customer-fixture', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
      }
      outgoing.end('data: [DONE]\n\n');
    });
    provider.listen(0, '127.0.0.1');
    await once(provider, 'listening');
    t.after(async () => {
      provider.closeAllConnections();
      await new Promise((resolve) => provider.close(resolve));
      await fs.rm(home, { force: true, recursive: true });
    });
    const template = JSON.parse(await fs.readFile(new URL('../../config/openclaw.template.json', import.meta.url)));
    const configs = {
      gateway: { mode: 'local', bind: 'loopback', auth: { mode: 'token', token: gatewayToken } },
      agents: { ownership: 'explicit', defaults: { model: { primary: 'openai/customer-fixture' }, timeoutSeconds: 30,
        thinkingDefault: 'off', systemAgent: { agentId: 'main' } },
        entries: {
          main: { workspace: path.join(home, 'admin-workspace') },
          public: { workspace: path.join(home, 'public-workspace'), tools: template.agents.entries.public.tools },
        } },
      models: { mode: 'merge', providers: { openai: {
        baseUrl: `http://127.0.0.1:${provider.address().port}/v1`, apiKey: 'fixture-key', api: 'openai-completions',
        models: [{ id: 'customer-fixture', name: 'Customer fixture', input: ['text'],
          contextWindow: 128000, maxTokens: 1024, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
      } } },
      plugins: { allow: ['openai', 'public-service-gate'],
        load: { paths: [plugin] },
        entries: { openai: { enabled: true }, 'public-service-gate': {
          enabled: true, hooks: template.plugins.entries['public-service-gate'].hooks,
          config: { enabled: true },
        } },
      },
      channels: {}, cron: { enabled: false },
    };
    const configPath = path.join(state, 'openclaw.json');
    await fs.writeFile(configPath, JSON.stringify(configs));
    const apiPort = await temporaryPort();
    const env = { ...process.env, HOME: home, OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: configPath,
      OPENCLAW_GATEWAY_TOKEN: gatewayToken, OPENAI_API_KEY: 'fixture-key', OPENCLAW_NO_AUTO_UPDATE: '1',
      PYTHONDONTWRITEBYTECODE: '1', CONSOLE_API_TOKEN: token, CONSOLE_API_BIND: '127.0.0.1', CONSOLE_API_PORT: String(apiPort) };
    delete env.OPENCLAW_HOME;
    const port = await temporaryPort();
    const api = start(t, 'python3', ['-u', fileURLToPath(new URL('./console-api.py', import.meta.url))], env);
    await waitFor(api, () => api.logs().includes('listening on'));
    const read = async (body) => {
      const response = await fetch(`http://127.0.0.1:${apiPort}/internal/console/customer-bot`, {
        method: body ? 'POST' : 'GET', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
      });
      const value = await response.json();
      assert.equal(response.status, 200, JSON.stringify(value));
      return value;
    };
    const gateway = start(t, process.execPath, [path.join(root, 'openclaw.mjs'), 'gateway',
      '--allow-unconfigured', '--port', String(port), '--bind', 'loopback'], env);
    await waitFor(gateway, () => gateway.logs().includes('[gateway] ready'));
    let resolveReady, rejectReady;
    const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
    const client = new GatewayClient({ url: `ws://127.0.0.1:${port}`, token: gatewayToken,
      clientName: 'gateway-client', mode: 'backend', role: 'operator', scopes: ['operator.admin'],
      sharedStateMode: 'read-only', deviceIdentity: null, onHelloOk: resolveReady, onConnectError: rejectReady });
    t.after(() => client.stopAndWait({ timeoutMs: 2000 }));
    client.start();
    await ready;
    const infer = async (agentId, target, isGroup = false) => {
      const previousCount = requests.length;
      const response = await client.request('agent', {
        message: 'What do the bricks cost?', agentId, to: target, channel: 'whatsapp', deliver: false,
        sessionKey: `agent:${agentId}:whatsapp:${isGroup ? 'group' : 'direct'}:${target}`,
        idempotencyKey: randomUUID(),
      }, { expectFinal: true, timeoutMs: 45_000 }).catch((error) => {
        throw new Error(`${error.message}\n${gateway.logs()}`, { cause: error });
      });
      assert.equal(response.status, 'ok', JSON.stringify(response));
      assert.ok(requests.length > previousCount, gateway.logs());
      return requests.at(-1).messages.filter((message) => message.role === 'system').map((message) => message.content).join('\n');
    };

    const initial = await read();
    const draft = { instructions: initial.instructions + '\nStart with the words: Fixture short reply.',
      knowledge: initial.knowledge.replace('$95 per 1000', '$107 per 1000'), expectedRevision: initial.revision,
      actor: 'Fixture operator' };
    const before = await infer('public', '+19995550123');
    assert.match(before, /\$95 per 1000/, gateway.logs());
    assert.doesNotMatch(before, /Fixture short reply|\$107 per 1000/);
    const published = await read(draft);
    assert.notEqual(published.revision, initial.revision);
    const after = await infer('public', '+19995550123');
    assert.match(after, /Fixture short reply/);
    assert.match(after, /\$107 per 1000/);
    assert.doesNotMatch(after, /\$95 per 1000/);
    assert.doesNotMatch(await infer('main', '+19995550124'), /Fixture short reply|\$107 per 1000/);
    assert.doesNotMatch(await infer('public', '1234@g.us', true), /Fixture short reply|\$107 per 1000/);

    // Corrupt active knowledge must not quietly bring back a shipped price.
    await fs.writeFile(path.join(state, 'runtime', 'customer-bot', 'published.json'), '{');
    const unavailable = await infer('public', '+19995550123');
    assert.match(unavailable, /use the handoff action instead/);
    assert.doesNotMatch(unavailable, /\$95 per 1000|\$107 per 1000/);
  });
