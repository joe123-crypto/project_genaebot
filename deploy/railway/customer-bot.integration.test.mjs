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

function isActivityRecap(call) {
  return call.messages.some((entry) =>
    entry.role === 'system' && String(entry.content).startsWith('Write an Activity recap'));
}

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

test('published instructions and fresh-lookup policy reach the next model call in the same customer WhatsApp session only',
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
    const publicWorkspace = path.join(home, 'public-workspace');
    await fs.mkdir(publicWorkspace, { recursive: true });
    // The fixture has no WhatsApp adapter. Supply the policy normally attached
    // to the WhatsApp turn by the boot-synced wildcard system prompt.
    await fs.copyFile(path.join(source, 'docs', 'public-whatsapp-system-prompt.txt'), path.join(publicWorkspace, 'AGENTS.md'));
    const legacyQuote = 'We make CVs for US$5.';
    const requests = [];
    const provider = http.createServer(async (incoming, outgoing) => {
      if (incoming.url === '/v1/models') {
        outgoing.writeHead(200, { 'content-type': 'application/json' });
        outgoing.end(JSON.stringify({ data: [{ id: 'customer-fixture', object: 'model' }] }));
        return;
      }
      let body = '';
      for await (const chunk of incoming) body += chunk;
      const call = JSON.parse(body);
      requests.push(call);
      outgoing.writeHead(200, { 'content-type': 'text/event-stream' });
      const lastUser = call.messages.findLast((message) => message.role === 'user');
      const reply = !isActivityRecap(call) && JSON.stringify(lastUser?.content ?? '').includes('What services do you offer?')
        ? legacyQuote : 'fixture-ok';
      const describe = JSON.stringify(lastUser?.content ?? '').includes('Describe current lookup tools')
        && !call.messages.slice(call.messages.lastIndexOf(lastUser) + 1).some((message) => message.role === 'tool');
      const deltas = describe
        ? [[{ role: 'assistant', tool_calls: [{ index: 0, id: 'describe-public-service', type: 'function',
          function: { name: 'tool_describe', arguments: JSON.stringify({ id: 'public_service' }) } }] }, null], [{}, 'tool_calls']]
        : [[{ role: 'assistant', content: reply }, null], [{}, 'stop']];
      for (const [delta, finish_reason] of deltas) {
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
          public: { workspace: publicWorkspace, tools: template.agents.entries.public.tools },
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
          config: { enabled: true, cvMakerEnabled: true },
        } },
      },
      channels: {}, cron: { enabled: false },
    };
    const configPath = path.join(state, 'openclaw.json');
    await fs.writeFile(configPath, JSON.stringify(configs));
    const apiPort = await temporaryPort();
    const env = { ...process.env, HOME: home, OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: configPath,
      OPENCLAW_GATEWAY_TOKEN: gatewayToken, OPENAI_API_KEY: 'fixture-key', OPENCLAW_NO_AUTO_UPDATE: '1',
      PYTHONDONTWRITEBYTECODE: '1', CONSOLE_API_TOKEN: token, CONSOLE_API_BIND: '127.0.0.1', CONSOLE_API_PORT: String(apiPort),
      GENAIE_CV_ACCEPT_NEW_ORDERS: '0' };
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
    const infer = async (agentId, target, isGroup = false, message = 'What do you sell and what do the bricks cost?') => {
      const previousCount = requests.length;
      const turnTag = `fixture-turn-${randomUUID()}`;
      const response = await client.request('agent', {
        message: `${message}\n${turnTag}`, agentId, to: target, channel: 'whatsapp', deliver: false,
        sessionKey: `agent:${agentId}:whatsapp:${isGroup ? 'group' : 'direct'}:${target}`,
        idempotencyKey: randomUUID(),
      }, { expectFinal: true, timeoutMs: 45_000 }).catch((error) => {
        throw new Error(`${error.message}\n${gateway.logs()}`, { cause: error });
      });
      assert.equal(response.status, 'ok', JSON.stringify(response));
      // Background recaps can include the turn's text as input. Select the
      // conversation request for this turn, excluding those recaps.
      const calls = requests.slice(previousCount);
      const call = calls.findLast((request) => !isActivityRecap(request) && JSON.stringify(request.messages).includes(turnTag));
      assert.ok(call, gateway.logs());
      return {
        system: call.messages.filter((message) => message.role === 'system').map((message) => message.content).join('\n'),
        priorReplies: call.messages.filter((message) => message.role === 'assistant'),
        tools: call.tools,
        messages: call.messages,
      };
    };

    const initial = await read();
    const draft = { instructions: initial.instructions + '\nStart with the words: Fixture short reply.',
      expectedRevision: initial.revision,
      actor: 'Fixture operator' };
    const { system: before, tools: beforeTools } = await infer('public', '+19995550123', false, 'What services do you offer?');
    assert.doesNotMatch(before, /\$95 per 1000/, gateway.logs());
    assert.match(before, /fresh public_service lookup in this turn/);
    assert.match(before, /campaign_info/);
    assert.doesNotMatch(before, /Fixture short reply|\$107 per 1000/);
    assert.match(before, /New CV orders are closed/);
    assert.match(before, /One or two short sentences is a whole reply/);
    assert.doesNotMatch(before, /CV[^\n]*\$5|\$5[^\n]*CV|cv_maker_start/);
    assert.doesNotMatch(JSON.stringify(beforeTools), /cv_maker_start/);
    const published = await read(draft);
    assert.notEqual(published.revision, initial.revision);
    const { system: after, priorReplies, tools: afterTools } = await infer('public', '+19995550123');
    assert.match(after, /Fixture short reply/);
    assert.doesNotMatch(after, /\$107 per 1000/);
    assert.match(after, /fresh public_service lookup in this turn/);
    assert.doesNotMatch(after, /\$95 per 1000/);
    assert.match(JSON.stringify(priorReplies), /We make CVs for US\$5\./);
    assert.match(after, /A past offer, quote, or acceptance in the conversation does not change this/);
    assert.doesNotMatch(after, /CV[^\n]*\$5|\$5[^\n]*CV|cv_maker_start/);
    assert.doesNotMatch(JSON.stringify(afterTools), /cv_maker_start/);
    assert.doesNotMatch((await infer('main', '+19995550124')).system, /Fixture short reply|\$107 per 1000/);
    assert.doesNotMatch((await infer('public', '1234@g.us', true)).system, /Fixture short reply|\$107 per 1000/);

    const described = await infer('public', '+19995550123', false, 'Describe current lookup tools');
    const toolResults = described.messages.filter((message) => message.role === 'tool');
    assert.ok(toolResults.length > 0, gateway.logs());
    assert.match(JSON.stringify(toolResults), /campaign_info/);
    assert.match(JSON.stringify(toolResults), /business_info/);
    assert.doesNotMatch(JSON.stringify(toolResults), /cv_maker_start/);

    // A corrupt publication cannot bring back a retired briefing price.
    await fs.writeFile(path.join(state, 'runtime', 'customer-bot', 'published.json'), '{');
    const { system: unavailable } = await infer('public', '+19995550123');
    assert.match(unavailable, /fresh public_service lookup in this turn/);
    assert.doesNotMatch(unavailable, /\$95 per 1000|\$107 per 1000/);
  });
