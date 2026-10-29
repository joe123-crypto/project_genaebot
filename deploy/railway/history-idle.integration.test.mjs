import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';

if (process.env.GENAIE_REQUIRE_INTEGRATION === '1' && !process.env.GENAIE_OPENCLAW_ROOT) {
  throw new Error('required history integration cannot skip');
}

test('real history owner retires only after release and recreates database readers twice',
  { skip: !process.env.GENAIE_OPENCLAW_ROOT, timeout: 60_000 }, async (t) => {
    const state = await fs.mkdtemp(path.join(os.tmpdir(), 'history-idle-'));
    const previousEnvironment = { ...process.env };
    process.env.OPENCLAW_STATE_DIR = state;
    delete process.env.OPENCLAW_HOME;
    process.env.GENAIE_HISTORY_IDLE_RETIREMENT = '1';
    const originalSet = globalThis.setTimeout;
    const originalClear = globalThis.clearTimeout;
    const timers = new Map();
    let db;
    let resources;
    let lifecycle;
    let stateCache;
    // Advance only the owner's five-minute idle deadline. All real worker I/O,
    // SQLite, native close acknowledgement and exit/join paths run unchanged.
    globalThis.setTimeout = (callback, ms, ...args) => {
      if (ms !== 300_000) return originalSet(callback, ms, ...args);
      const handle = originalSet(() => {}, 3_600_000);
      timers.set(handle, () => callback(...args));
      return handle;
    };
    globalThis.clearTimeout = (handle) => { timers.delete(handle); return originalClear(handle); };
    t.after(async () => {
      if (resources) await Promise.all([resources.s, resources.l, resources.c, resources.i, resources.a].map((lane) => lane.pool.close()));
      if (lifecycle) await lifecycle.s();
      if (stateCache) await stateCache.a();
      if (db) await db.r();
      for (const timer of timers.keys()) originalClear(timer);
      globalThis.setTimeout = originalSet;
      globalThis.clearTimeout = originalClear;
      for (const key of Object.keys(process.env)) if (!(key in previousEnvironment)) delete process.env[key];
      Object.assign(process.env, previousEnvironment);
      await fs.rm(state, { recursive: true, force: true });
    });
    const root = path.join(process.env.GENAIE_OPENCLAW_ROOT, 'dist');
    const files = await fs.readdir(root);
    async function load(prefix, evidence) {
      for (const name of files.filter((file) => file.startsWith(prefix) && file.endsWith('.mjs'))) {
        if ((await fs.readFile(path.join(root, name), 'utf8')).includes(evidence)) {
          return import(pathToFileURL(path.join(root, name)));
        }
      }
      throw new Error(`missing installed owner ${prefix}`);
    }
    db = await load('openclaw-agent-db-', 'function openOpenClawAgentDatabase(');
    lifecycle = await load('openclaw-agent-db-lifecycle-', 'async function closeOpenClawAgentDatabasesAsync(');
    stateCache = await load('openclaw-state-db-cache-', 'async function closeOpenClawStateDatabaseAsync(');
    const runtime = await load('session-transcript-worker-runtime-', 'function withSessionHistoryWorkerDatabase(');
    resources = await load('session-transcript-worker-resources-', 'function armDatabaseWorkerIdleRetirement(');
    const options = { agentId: 'main', env: { ...process.env } };
    const lanes = [resources.s, resources.l, resources.c];
    await db.p(options, (database) => database.db.exec(
      "CREATE TABLE memory_trial_fixture(value TEXT); INSERT INTO memory_trial_fixture VALUES ('preserved')"));
    const entries = await load('session-accessor.sqlite-entry-', 'async function replaceSessionEntry(');
    const scope = { agentId: 'main', sessionKey: 'agent:main:memory-trial', env: options.env };
    await entries.y(scope, { sessionId: 'memory-trial-session', updatedAt: Date.now(), label: 'Preserved conversation' },
      { skipMaintenance: true });
    for (const lane of lanes) {
      for (let cycle = 1; cycle <= 2; cycle++) {
        await runtime.o(options, async (owner) => {
          assert.equal(lane.pending, 1);
          assert.equal(timers.has(lane.idleTimer), false, 'admitted work clears idle retirement');
          await owner.prewarm({ env: options.env });
          const entry = await owner.readEntryResult({ scope });
          assert.equal(entry.ok, true);
          assert.equal(entry.value.sessionId, 'memory-trial-session');
          assert.equal(entry.value.label, 'Preserved conversation');
          assert.equal(lane.pool.getSnapshot().workers, 1);
          await new Promise((resolve) => originalSet(resolve, 20));
          assert.equal(lane.pool.getSnapshot().workers, 1, 'active scope retains its reader');
        }, lane);
        const timer = lane.idleTimer;
        assert.ok(timers.has(timer), 'five-minute history timer is armed after owner release');
        const callback = timers.get(timer);
        timers.delete(timer);
        originalClear(timer);
        callback();
        await lane.rotation;
        assert.equal(lane.pool.getSnapshot().workers, 0, 'native worker exit is joined');
        assert.equal(lane.pool.getSnapshot().workersCreated, cycle);
        assert.equal(lane.retiredSequence, lane.nativeSequence, 'database custody is released after exit');
      }
    }
    // Usage-refresh/writer policy remains upstream; disabling the trial restores
    // the history owner's original 30-minute retention as well.
    await runtime.o(options, (owner) => owner.prewarm({ env: options.env }), resources.i);
    assert.equal(timers.has(resources.i.idleTimer), false);
    process.env.GENAIE_HISTORY_IDLE_RETIREMENT = '0';
    await runtime.o(options, (owner) => owner.prewarm({ env: options.env }));
    assert.equal(timers.has(resources.s.idleTimer), false);
    await db.p(options, (database) => {
      assert.equal(database.db.prepare('SELECT value FROM memory_trial_fixture').get().value, 'preserved');
      assert.equal(database.db.prepare('PRAGMA quick_check').get().quick_check, 'ok');
    });
  });
