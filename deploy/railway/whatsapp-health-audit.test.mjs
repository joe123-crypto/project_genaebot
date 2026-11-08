import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createWhatsAppHealthAudit } from './whatsapp-health-audit.mjs';
import { patchWhatsAppHealthAudit, patchWhatsAppGroupDiscovery } from './whatsapp-lid-loader.mjs';

const upstream = fs.readFileSync(new URL('./fixtures/whatsapp-lifecycle-2026.9.9.js', import.meta.url), 'utf8');

function journal(t, overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'whatsapp-health-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const warnings = [];
  const options = { root, accountId: 'ads', runId: 'run-1',
    environment: { RAILWAY_DEPLOYMENT_ID: 'deployment-1' },
    now: () => new Date('2026-11-04T09:00:00Z'), warn: (s) => warnings.push(s), ...overrides };
  const directory = path.join(root, 'runtime/whatsapp-health');
  return { root, directory, warnings, write: createWhatsAppHealthAudit(options),
    records: () => fs.readdirSync(directory).filter(n => n.endsWith('.jsonl'))
      .flatMap(n => fs.readFileSync(path.join(directory, n), 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse)) };
}

test('journal records account lifecycle and excludes untrusted payloads', (t) => {
  const f = journal(t);
  f.write({ event: 'disconnected', statusCode: 401, connectionId: 'connection-1',
    reason: 'secret customer message', error: 'secret credentials', to: '+123456789' });
  const [record] = f.records();
  assert.equal(record.reason, 'authentication');
  assert.equal(record.accountId, 'ads');
  assert.equal(record.deploymentId, 'deployment-1');
  assert.equal(record.connectionId, 'connection-1');
  assert.equal(record.planned, false);
  assert.doesNotMatch(JSON.stringify(record), /secret|123456789/);
  const file = path.join(f.directory, 'events-2026-11-04.jsonl');
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
});

test('retention prunes only dated audit files and limits daily storage', (t) => {
  const f = journal(t, { maxDayBytes: 500 });
  fs.mkdirSync(f.directory, { recursive: true });
  fs.writeFileSync(path.join(f.directory, 'events-2026-10-21.jsonl'), 'old\n');
  fs.writeFileSync(path.join(f.directory, 'events-2026-10-22.jsonl'), '');
  fs.writeFileSync(path.join(f.directory, 'keep.txt'), 'untouched');
  for (let i = 0; i < 20; i++) f.write({ event: 'connected' });
  assert.equal(fs.existsSync(path.join(f.directory, 'events-2026-10-21.jsonl')), false);
  assert.equal(fs.existsSync(path.join(f.directory, 'events-2026-10-22.jsonl')), true);
  assert.equal(fs.readFileSync(path.join(f.directory, 'keep.txt'), 'utf8'), 'untouched');
  assert.ok(fs.statSync(path.join(f.directory, 'events-2026-11-04.jsonl')).size <= 500);
  assert.equal(f.warnings.length, 1);
});

test('opt-out creates no files and storage failure does not affect callers', (t) => {
  const f = journal(t, { environment: { GENAIE_WHATSAPP_HEALTH_AUDIT: '0' } });
  f.write({ event: 'connected' });
  assert.equal(fs.existsSync(f.directory), false);
  const file = path.join(f.root, 'not-a-directory'); fs.writeFileSync(file, 'x');
  const write = createWhatsAppHealthAudit({ root: file, accountId: 'ads', warn() {} });
  assert.doesNotThrow(() => write({ event: 'connected' }));
});

// Execute the unmodified upstream monitor body plus our instrumentation with
// controlled transport dependencies. No real socket, login, or credentials.
async function monitor({ setup = false, planned = false, watchdog = false, discovery = false } = {}) {
  const records = [], actions = [];
  const signal = { aborted: false };
  const logger = { info() {}, warn() {}, raw() {} };
  class Controller {
    constructor(options) { actions.push(['controller', options]); }
    async openConnection(options) {
      actions.push(['open']);
      if (setup) throw new Error('fixture authentication failure');
      if (watchdog) options.onWatchdogTimeout({ connectionId: 'connection-1',
        startedAt: Date.now(), lastTransportActivityAt: Date.now(), handledMessages: 0 });
      return { connectionId: 'connection-1' };
    }
    resolveSetupErrorDecision() { return { normalized: { statusCode: 401, statusLabel: '401' },
      healthState: 'logged-out', reconnectAttempts: 0, action: 'stop' }; }
    async waitForClose() { signal.aborted = planned; return planned ? 'aborted' : 'fixture-close'; }
    resolveCloseDecision() { return { normalized: { statusCode: 440, statusLabel: '440' },
      healthState: 'conflict', reconnectAttempts: 0, action: 'stop' }; }
    getReconnectAttempts() { return 0; }
    getCurrentSock() { return 'fixture-existing-socket'; }
    setUnhandledRejectionCleanup() {}
    async closeCurrentConnection() { actions.push(['close']); }
    async shutdown() { actions.push(['shutdown']); }
  }
  const scope = {
    attachWebInboxToSocket() {}, defaultRuntime: { error() {}, log() {} },
    newConnectionId: () => 'connection-1', getChildLogger$1: () => logger,
    resolveWebMonitorConfigSnapshot: () => ({ cfg: {}, account: { accountId: 'ads', authDir: '/fixture' } }),
    getRuntimeConfig$1: () => ({}), resolveWhatsAppMediaMaxBytes: () => 1,
    resolveHeartbeatSeconds: () => 60, resolveReconnectPolicy: () => ({ maxAttempts: 12 }),
    resolveWhatsAppSocketTiming: () => ({}), resolvePromptHistoryLimit: () => 0,
    DEFAULT_TRANSPORT_TIMEOUT_MS: 300_000, WHATSAPP_RECONNECT_CATCH_UP_MAX_MS: 1200_000,
    WhatsAppConnectionController: Controller,
    createWebChannelStatusController: () => new Proxy({}, { get: () => () => {} }),
    genaieCreateWhatsAppHealthAudit: ({ accountId, runId }) => (data) => records.push({ accountId, runId, ...data }),
    genaieCreateWhatsAppGroupDiscovery: ({ accountId }) => ({
      start: socket => actions.push(['discovery-start', accountId, socket]),
      stop: () => actions.push(['discovery-stop']),
    }),
    registerChannelRuntimeContext: () => ({ dispose() {} }),
    CHANNEL_APPROVAL_NATIVE_RUNTIME_CONTEXT_CAPABILITY: 'fixture',
    registerUnhandledRejectionHandler: () => () => {}, readWebSelfId: () => ({}),
    resolveAgentRoute: () => ({ sessionKey: 'fixture' }), enqueueSystemEvent() {},
    normalizeReconnectAccountId: (x) => x, drainPendingDeliveries: async () => {},
    resolveWhatsAppInboundPolicy: () => ({ account: {}, groupAllowFrom: [] }),
    formatWhatsAppInboundListeningLog: () => 'listening', whatsappLog: logger,
    whatsappHeartbeatLog: logger, formatCliCommand: (x) => x, formatError: (e) => String(e),
    setInterval: () => 1, clearInterval() {},
  };
  const patched = patchWhatsAppHealthAudit(upstream);
  const source = (discovery ? patchWhatsAppGroupDiscovery(patched) : patched).split('\n').filter(l => !l.startsWith('import ')).join('\n');
  const proxy = new Proxy(scope, { has: () => true, get: (o, k) => k in o ? o[k] : globalThis[k] });
  const run = new Function('scope', `with (scope) { ${source}; return monitorWebChannel; }`)(proxy);
  await run(false, undefined, true, () => {}, undefined, signal);
  return { records, actions };
}

test('real monitor journals setup authentication rejection without retrying', async () => {
  const { records, actions } = await monitor({ setup: true });
  assert.deepEqual(records.map(r => r.event), ['connect_attempt', 'setup_failure', 'stopped']);
  assert.equal(records[1].statusCode, 401);
  assert.equal(records[1].reason, 'logged-out');
  assert.equal(actions.filter(a => a[0] === 'open').length, 1);
});

test('real monitor distinguishes watchdog, session conflict, and planned stop', async () => {
  const conflict = await monitor({ watchdog: true });
  assert.deepEqual(conflict.records.map(r => r.event), ['connect_attempt', 'watchdog', 'connected', 'disconnected', 'stopped']);
  assert.equal(conflict.records[1].reason, 'app-silent');
  assert.equal(conflict.records[3].statusCode, 440);
  assert.equal(conflict.records[3].reason, 'conflict');
  assert.equal(conflict.actions[0][1].transportTimeoutMs, 300_000);
  assert.equal(conflict.actions[0][1].messageTimeoutMs, 1800_000);
  const stop = await monitor({ planned: true });
  assert.deepEqual(stop.records.map(r => r.event), ['connect_attempt', 'connected', 'stopped']);
  assert.equal(stop.records.at(-1).planned, true);
  assert.equal(stop.records.at(-1).reason, 'shutdown');
});

test('audit patch is idempotent and rejects an incompatible upstream monitor', () => {
  const patched = patchWhatsAppHealthAudit(upstream);
  assert.equal(patchWhatsAppHealthAudit(patched), patched);
  assert.throws(() => patchWhatsAppHealthAudit(upstream.replace('statusController.noteConnected();', 'changed();')), /contract changed/);
});

test('actual monitor starts discovery on its existing socket and stops it on disconnect and shutdown', async () => {
  const { actions } = await monitor({ discovery: true });
  assert.deepEqual(actions.filter(a => a[0] === 'discovery-start'), [['discovery-start', 'ads', 'fixture-existing-socket']]);
  assert.equal(actions.filter(a => a[0] === 'discovery-stop').length, 2);
  assert.ok(actions.findIndex(a => a[0] === 'discovery-stop') < actions.findIndex(a => a[0] === 'shutdown'));
  const failed = await monitor({ discovery: true, setup: true });
  assert.equal(failed.actions.some(a => a[0] === 'discovery-start'), false);
  assert.equal(failed.actions.at(-2)[0], 'discovery-stop');
  const aborted = await monitor({ discovery: true, planned: true });
  assert.equal(aborted.actions.filter(a => a[0] === 'discovery-stop').length, 2);
});
