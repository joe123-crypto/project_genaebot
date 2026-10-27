// Run against the actual pinned pool with GENAIE_OPENCLAW_ROOT set. No gateway,
// credentials or production database is used; the worker echoes fixture input.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';

test('installed pool joins idle workers, preserves active work and recreates a worker',
  { skip: !process.env.GENAIE_OPENCLAW_ROOT }, async (t) => {
    const root = process.env.GENAIE_OPENCLAW_ROOT;
    const dist = path.join(root, 'dist');
    const files = (await fs.readdir(dist)).filter((file) => /^worker-task-pool-.*\.mjs$/.test(file));
    assert.equal(files.length, 1);
    const source = await fs.readFile(path.join(dist, files[0]), 'utf8');
    const symbol = /WorkerTaskPool as (\w+)/.exec(source)?.[1];
    assert.ok(symbol, 'pinned pool export changed');
    const Pool = (await import(pathToFileURL(path.join(dist, files[0]))))[symbol];
    const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'pool-retirement-'));
    t.after(() => fs.rm(temporary, { recursive: true, force: true }));
    const worker = path.join(temporary, 'echo.mjs');
    await fs.writeFile(worker, `import {parentPort} from 'node:worker_threads';
parentPort.on('message', m => {
 if (!('taskId' in m) || !('input' in m)) return;
 setTimeout(() => parentPort.postMessage({taskId:m.taskId,status:'ok',value:m.input.value}),m.input.delay);
});`);
    let released = 0;
    const pool = new Pool({ workerUrl: pathToFileURL(worker), maxWorkers: 1,
      idleTimeoutMs: 20, prepareWorker: () => ({ releaseResources: () => { released++; } }) });
    t.after(() => pool.close());
    assert.equal(await pool.run({ value: 'first', delay: 150 }, {}), 'first');
    const deadline = Date.now() + 5000;
    while (pool.getSnapshot().workers && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    assert.equal(pool.getSnapshot().workers, 0);
    assert.equal(released, 1);
    assert.equal(await pool.run({ value: 'after-idle', delay: 150 }, {}), 'after-idle');
    assert.equal(pool.getSnapshot().workersCreated, 2);
    assert.equal(pool.getSnapshot().activeTasks, 0);
    await pool.close();
    assert.equal(released, 2);
  });
