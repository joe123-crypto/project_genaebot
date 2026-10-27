import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test from 'node:test';

const root = process.env.GENAIE_OPENCLAW_ROOT;
if (process.env.GENAIE_REQUIRE_INTEGRATION === '1' && !root) {
  throw new Error('required installed-runtime integration cannot skip');
}
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(read, accept, timeout, label) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    last = await read();
    if (accept(last)) return last;
    await delay(500);
  }
  throw new Error(`${label} timed out: ${JSON.stringify(last)}`);
}

test('real catalog/auth recovery and model inference survive two retirements; opt-out retains workers',
  { skip: !root, timeout: 900_000 }, async (t) => {
    const { GatewayClient } = await import(pathToFileURL(path.join(root, 'dist/plugin-sdk/gateway-runtime.js')));
    const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'catalog-recovery-'));
    const requests = [];
    let deny = false;
    const provider = http.createServer(async (request, response) => {
      let body = '';
      for await (const chunk of request) body += chunk;
      requests.push({ route: request.url, auth: request.headers.authorization, body });
      if (deny) {
        response.writeHead(401, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ error: { message: 'fixture-denied', type: 'authentication_error' } }));
        return;
      }
      if (request.url === '/v1/models') {
        response.setHeader('content-type', 'application/json');
        response.end(JSON.stringify({ object: 'list', data: [{ id: 'fixture-model', object: 'model' }] }));
        return;
      }
      assert.equal(request.url, '/v1/chat/completions');
      assert.equal(request.headers.authorization, 'Bearer fixture-model-key-v1');
      response.writeHead(200, { 'content-type': 'text/event-stream' });
      for (const [delta, finish_reason] of [[{ role: 'assistant', content: 'fixture-ok' }, null], [{}, 'stop']]) {
        response.write('data: ' + JSON.stringify({ id: 'fixture-response', object: 'chat.completion.chunk',
          created: 1, model: 'fixture-model', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
      }
      response.end('data: [DONE]\n\n');
    });
    provider.listen(0, '127.0.0.1');
    await once(provider, 'listening');
    const gateways = [];
    t.after(async () => {
      for (const fixture of gateways) await fixture.close();
      await new Promise((resolve) => provider.close(resolve));
      await fs.rm(temporary, { recursive: true, force: true });
    });
    async function gateway(name, retirement) {
      const home = path.join(temporary, name);
      const state = path.join(home, 'state');
      await fs.mkdir(state, { recursive: true });
      const controlFile = path.join(home, 'control.json');
      const config = {
        gateway: { mode: 'local', bind: 'loopback', auth: { mode: 'token', token: 'fixture-gateway-token' } },
        agents: { defaults: { model: { primary: 'openai/fixture-model' },
          timeoutSeconds: 30, thinkingDefault: 'off', workspace: path.join(home, 'workspace') },
          entries: { main: { workspace: path.join(home, 'workspace') } } },
        models: { mode: 'merge', providers: { openai: {
          baseUrl: `http://127.0.0.1:${provider.address().port}/v1`, apiKey: 'fixture-model-key-v1',
          api: 'openai-completions', models: [{ id: 'fixture-model', name: 'Fixture model',
            reasoning: false, input: ['text'], contextWindow: 128000, maxTokens: 1024,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
        } } },
        plugins: { allow: ['openai'], entries: { openai: { enabled: true } } },
        channels: {}, tools: { deny: ['message'] }, cron: { enabled: false },
      };
      const configPath = path.join(state, 'openclaw.json');
      await fs.writeFile(configPath, JSON.stringify(config));
      const probe = http.createServer();
      probe.listen(0, '127.0.0.1');
      await once(probe, 'listening');
      const port = probe.address().port;
      await new Promise((resolve) => probe.close(resolve));
      const env = { ...process.env, HOME: home, OPENCLAW_STATE_DIR: state,
        OPENCLAW_CONFIG_PATH: configPath, OPENCLAW_GATEWAY_TOKEN: 'fixture-gateway-token',
        OPENAI_API_KEY: 'fixture-model-key-v1', GENAIE_CATALOG_IDLE_RETIREMENT: retirement,
        GENAIE_CATALOG_FIXTURE_CONTROL: controlFile, OPENCLAW_NO_AUTO_UPDATE: '1',
        NODE_OPTIONS: `--import=${fileURLToPath(new URL('./catalog-fixture-control.mjs', import.meta.url))}` };
      delete env.OPENCLAW_HOME;
      const processHandle = spawn(process.execPath, [path.join(root, 'openclaw.mjs'), 'gateway',
        '--allow-unconfigured', '--port', String(port), '--bind', 'loopback'], { env });
      let logs = '';
      processHandle.stdout.on('data', (chunk) => { logs = (logs + chunk).slice(-20000); });
      processHandle.stderr.on('data', (chunk) => { logs = (logs + chunk).slice(-20000); });
      let client;
      const fixture = {
        async close() {
          await client?.stopAndWait({ timeoutMs: 2000 });
          if (processHandle.exitCode === null && !processHandle.signalCode) {
            processHandle.kill('SIGTERM');
            await Promise.race([once(processHandle, 'exit'), delay(10_000)]);
            if (processHandle.exitCode === null && !processHandle.signalCode) processHandle.kill('SIGKILL');
          }
        },
      };
      gateways.push(fixture);
      await until(async () => {
        if (processHandle.exitCode !== null) throw new Error(`fixture gateway exited: ${logs}`);
        return logs;
      }, (text) => text.includes('[gateway] ready'), 90_000, 'fixture startup');
      const control = JSON.parse(await fs.readFile(controlFile, 'utf8'));
      fixture.control = async (command) => {
        const response = await fetch(`http://127.0.0.1:${control.port}/${command}`);
        const value = await response.json();
        assert.equal(response.ok, true, JSON.stringify(value));
        return value;
      };
      let resolveReady;
      let rejectReady;
      const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
      client = new GatewayClient({ url: `ws://127.0.0.1:${port}`, token: 'fixture-gateway-token',
        clientName: 'gateway-client', mode: 'backend', role: 'operator', scopes: ['operator.admin'],
        sharedStateMode: 'read-only', deviceIdentity: null,
        onHelloOk: resolveReady, onConnectError: rejectReady });
      client.start();
      await ready;
      await client.request('models.list', {}, { timeoutMs: 45_000 });
      fixture.infer = async () => {
        const previous = requests.length;
        const response = await client.request('agent', { message: 'Reply fixture-ok.', agentId: 'main',
          sessionKey: `agent:main:fixture:${randomUUID()}`, deliver: false, idempotencyKey: randomUUID() },
        { expectFinal: true, timeoutMs: 45_000 });
        assert.equal(response.status, 'ok', JSON.stringify(response));
        assert.ok(requests.length > previous, 'a real fixture provider request must occur');
        return response;
      };
      return fixture;
    }
    const fixture = await gateway('retirement', '1');
    await fixture.infer();
    console.log('fixture catalog bootstrap', await fixture.control('snapshot'));
    assert.equal((await fixture.control('catalog')).fixtureModel, true);
    assert.equal((await fixture.control('auth')).fixtureCredential, true);
    let created = (await fixture.control('snapshot')).pool.workersCreated;
    for (let cycle = 0; cycle < 2; cycle++) {
      const idle = await until(() => fixture.control('snapshot'), (value) => value.pool.workers === 0 &&
        value.captures.every((capture) => !capture.exists), 380_000, 'real catalog retirement/cleanup');
      assert.equal(idle.pool.activeTasks, 0);
      assert.equal((await fixture.control('catalog')).fixtureModel, true);
      assert.equal((await fixture.control('auth')).fixtureCredential, true);
      const recreated = await fixture.control('snapshot');
      assert.ok(recreated.pool.workersCreated > created, 'catalog request must create a fresh real worker');
      created = recreated.pool.workersCreated;
      await fixture.infer();
      console.log(`real catalog cycle ${cycle + 1}: recreated, authenticated, inferred`);
    }
    deny = true;
    await assert.rejects(fixture.infer(), /fixture-denied|401|error/i);
    deny = false;
    await fixture.close();
    const rollback = await gateway('retirement-disabled', '0');
    await rollback.infer();
    assert.equal((await rollback.control('catalog')).fixtureModel, true);
    await delay(70_000);
    const retained = await rollback.control('snapshot');
    assert.equal(retained.pool.workers, 1, 'opt-out must preserve the warm catalog worker');
    assert.equal(retained.pool.workersCreated, 1);
  });
