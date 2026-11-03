// Explicit, counter-only operator probe for the pinned runtime. Never preload
// this in the gateway: it opens a temporary loopback inspector, samples the
// existing direct Workers, disconnects, and verifies inspector shutdown.
import fs from 'node:fs';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const pid = Number(process.argv[1]); // Executed with node --input-type=module -e.
const root = process.env.GENAIE_OPENCLAW_ROOT ?? '/usr/local/lib/node_modules/openclaw';
const endpoint = 'http://127.0.0.1:9229/json/list';
if (!Number.isSafeInteger(pid) || pid <= 0) throw Error('A gateway PID is required');
if (JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).version !== '2026.9.9') {
  throw Error('Review worker-memory registry compatibility before changing OpenClaw');
}
if (!fs.readFileSync(`/proc/${pid}/comm`, 'utf8').startsWith('openclaw-gatewa')) throw Error('Unexpected process');
const owners = fs.readdirSync(path.join(root, 'dist')).filter(name =>
  /^session-transcript-worker-resources-.*\.mjs$/.test(name) &&
  fs.readFileSync(path.join(root, 'dist', name), 'utf8').includes('function armDatabaseWorkerIdleRetirement('));
if (owners.length !== 1) throw Error('History owner contract changed');
let targets;
try { targets = await (await fetch(endpoint, { signal: AbortSignal.timeout(500) })).json(); } catch {}
if (targets) throw Error('An inspector is already running; left untouched');
process.kill(pid, 'SIGUSR1');
for (let attempt = 0; attempt < 30 && !targets; attempt++) {
  await delay(100);
  try { targets = await (await fetch(endpoint, { signal: AbortSignal.timeout(500) })).json(); } catch {}
}
if (!targets?.[0]?.webSocketDebuggerUrl?.startsWith('ws://127.0.0.1:9229/')) {
  throw Error('Temporary inspector unavailable; check loopback port 9229');
}
const ws = new WebSocket(targets[0].webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(Error('Inspector connection timed out')), 3000);
  ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
  ws.addEventListener('error', () => { clearTimeout(timer); reject(Error('Inspector connection failed')); }, { once: true });
});
let nextId = 0;
const pending = new Map();
ws.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  const request = pending.get(message.id);
  if (!request) return;
  pending.delete(message.id);
  if (message.error) request.reject(Error('Inspector request failed'));
  else request.resolve(message.result);
});
function call(method, params) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => { pending.delete(id); reject(Error('Inspector request timed out')); }, 8000);
    pending.set(id, {
      resolve: value => { clearTimeout(timer); resolve(value); },
      reject: error => { clearTimeout(timer); reject(error); },
    });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
let ownsInspector = false;
try {
  const check = await call('Runtime.evaluate', { expression: 'process.pid', returnByValue: true });
  if (check.result.value !== pid) throw Error('Inspector PID mismatch; endpoint left untouched');
  ownsInspector = true;
  // Survives a disconnected/killed observer. No debugger pause or heap dump.
  await call('Runtime.evaluate', { expression:
    "setTimeout(() => process.getBuiltinModule('node:inspector').close(), 12000).unref(); undefined",
    returnByValue: true });
  const result = await call('Runtime.evaluate', { awaitPromise: true, returnByValue: true, expression: `(async () => {
    const registry = globalThis[Symbol.for('openclaw.workerCpuSources')];
    if (!(registry?.workers instanceof Map)) throw Error('Worker registry unavailable');
    const workers = await Promise.all([...registry.workers].map(async ([worker, source]) => {
      const row = {script: source.script, threadId: worker.threadId, poolId: source.poolId};
      let timer;
      try {
        const heap = await Promise.race([worker.getHeapStatistics(), new Promise((_, reject) => {
          timer = setTimeout(() => reject(Error('timeout')), 2000);
        })]);
        return {...row, heapUsed: heap.used_heap_size, heapTotal: heap.total_heap_size, external: heap.external_memory};
      } catch { return {...row, unavailable: true}; }
      finally { clearTimeout(timer); }
    }));
    const resources = process.getBuiltinModule('node:module').createRequire(${JSON.stringify(path.join(root, 'openclaw.mjs'))})(${JSON.stringify('./dist/' + owners[0])});
    const lanes = [resources.s, resources.l, resources.c, resources.i, resources.a].map(lane => ({
      name: lane.name, pending: lane.pending, nativeSequence: lane.nativeSequence,
      retiredSequence: lane.retiredSequence, pool: lane.pool.getSnapshot()
    }));
    return {sampledAt: new Date().toISOString(), pid: process.pid, memory: process.memoryUsage(), workers, lanes};
  })()` });
  if (result.exceptionDetails) throw Error('Counter sampling failed');
  // Print only after confirmed cleanup, so a caller cannot mistake a failed
  // shutdown for a successful capture.
  targets = result.result.value;
} finally {
  ws.close();
  if (ownsInspector) {
    await delay(13000);
    let listening = false;
    try { await fetch(endpoint, { signal: AbortSignal.timeout(500) }); listening = true; } catch {}
    if (listening) throw Error('Temporary inspector failed to close; check loopback port 9229');
  }
}
console.log(JSON.stringify({ ...targets, inspectorClosed: true,
  note: 'Direct worker heaps only; nested workers excluded. External includes ArrayBuffers. ' +
        'RSS also contains native allocations, code and allocator retention; the remainder is not automatically a leak.' }));
