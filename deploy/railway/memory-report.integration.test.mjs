import assert from 'node:assert/strict';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const run = promisify(execFile);
const root = process.env.GENAIE_OPENCLAW_ROOT;
const repo = fileURLToPath(new URL('../../', import.meta.url));
if (process.env.GENAIE_REQUIRE_INTEGRATION === '1' && !root) throw Error('Required memory probe cannot skip');

test('counter probe closes its inspector and real history reads survive source-cache advice',
  { skip: !root, timeout: 60_000 }, async t => {
    const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'memory-probe-'));
    const environment = { ...process.env, HOME: temporary, OPENCLAW_STATE_DIR: temporary,
      GENAIE_OPENCLAW_ROOT: root };
    delete environment.OPENCLAW_HOME;
    const fixture = `
      import fs from 'node:fs/promises';
      import path from 'node:path';
      import {pathToFileURL} from 'node:url';
      process.title = 'openclaw-gateway';
      const keepAlive = setInterval(() => {}, 1000);
      const dist = path.join(process.env.GENAIE_OPENCLAW_ROOT, 'dist');
      const files = await fs.readdir(dist);
      async function load(prefix, evidence) {
        for (const name of files.filter(f => f.startsWith(prefix) && f.endsWith('.mjs'))) {
          if ((await fs.readFile(path.join(dist, name), 'utf8')).includes(evidence)) {
            return import(pathToFileURL(path.join(dist, name)));
          }
        }
        throw Error('Pinned owner missing');
      }
      const entries = await load('session-accessor.sqlite-entry-', 'async function replaceSessionEntry(');
      const runtime = await load('session-transcript-worker-runtime-', 'function withSessionHistoryWorkerDatabase(');
      const options = {agentId:'main', env:{...process.env}};
      const scope = {...options, sessionKey:'agent:main:memory-probe'};
      await entries.y(scope, {sessionId:'preserved', updatedAt:Date.now(), label:'Private fixture text'}, {skipMaintenance:true});
      async function read() {
        return runtime.o(options, async owner => {
          const result = await owner.readEntryResult({scope});
          if (!result.ok) throw Error('Read failed');
          return result.value.sessionId;
        });
      }
      await read();
      process.on('message', async () => process.send(await read()));
      process.send('ready');
    `;
    const child = spawn(process.execPath, ['--input-type=module', '-e', fixture], {
      env: environment, stdio: ['ignore', 'ignore', 'pipe', 'ipc'],
    });
    let errors = '';
    child.stderr.on('data', data => { errors = (errors + data).slice(-2000); });
    t.after(async () => {
      if (child.exitCode === null) {
        child.kill('SIGTERM');
        await new Promise(resolve => child.once('exit', resolve));
      }
      await fs.rm(temporary, { recursive: true, force: true });
    });
    const message = () => new Promise((resolve, reject) => {
      const timer = setTimeout(() => { cleanup(); reject(Error('Fixture timed out: ' + errors)); }, 15_000);
      const cleanup = () => { clearTimeout(timer); child.off('message', success); child.off('exit', exit); };
      const success = value => { cleanup(); resolve(value); };
      const exit = () => { cleanup(); reject(Error('Fixture exited: ' + errors)); };
      child.once('message', success);
      child.once('exit', exit);
    });
    assert.equal(await message(), 'ready');
    const source = await fs.readFile(path.join(repo, 'deploy/railway-worker-memory.mjs'), 'utf8');
    const probe = await run(process.execPath, ['--input-type=module', '-e', source, '--', String(child.pid)], {
      env: environment, timeout: 25_000, maxBuffer: 1024 * 1024,
    });
    const counters = JSON.parse(probe.stdout);
    assert.equal(counters.pid, child.pid);
    assert.equal(counters.inspectorClosed, true);
    assert.ok(counters.memory.heapUsed > 0);
    assert.ok(counters.workers.some(worker => worker.script === 'session-transcript.worker.js' && worker.heapUsed > 0));
    assert.equal(probe.stdout.includes('Private fixture text'), false);
    await assert.rejects(fetch('http://127.0.0.1:9229/json/list', { signal: AbortSignal.timeout(500) }));
    const advice = await run('python3', ['-c', `import importlib.util; s=importlib.util.spec_from_file_location('memory',${JSON.stringify(path.join(repo, 'deploy/railway-memory-report.py'))}); m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.release_source_cache())`],
      { env: environment, timeout: 25_000 });
    assert.match(advice.stdout, /filesAdvised/);
    const after = message();
    child.send('read-again');
    assert.equal(await after, 'preserved');
  });
