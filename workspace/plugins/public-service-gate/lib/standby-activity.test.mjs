import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { trackStandbyActivity } from './standby-activity.js';

test('standby tracks concurrent CV turns, errors, and blocks new work during drain', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'standby-activity-'));
  try {
    let finish;
    const delayed = new Promise((resolve) => { finish = resolve; });
    const handler = trackStandbyActivity(async (fail) => {
      await delayed;
      if (fail) throw new Error('fixture failure');
      return { handled: true, text: 'done' };
    }, { GENAIE_OVERNIGHT_STANDBY: '1', GENAIE_STANDBY_RUNTIME: directory });
    const read = () => JSON.parse(fs.readFileSync(path.join(directory, 'requests.json')));
    assert.equal(read().active, 0);
    const first = handler(false);
    const second = assert.rejects(handler(true), /fixture failure/);
    assert.equal(read().active, 2);
    fs.writeFileSync(path.join(directory, 'closing'), '');
    assert.match((await handler(false)).text, /5:45/);
    assert.equal(read().active, 2);
    finish();
    assert.equal((await first).text, 'done');
    await second;
    assert.equal(read().active, 0);
    assert.equal(read().pid, process.pid);
    assert.equal(fs.statSync(path.join(directory, 'requests.json')).mode & 0o777, 0o600);
    fs.unlinkSync(path.join(directory, 'closing'));
    assert.equal((await handler(false)).text, 'done');
  } finally { fs.rmSync(directory, { recursive: true }); }
});

test('disabled standby returns the original callback without filesystem changes', () => {
  const handler = () => 'unchanged';
  assert.equal(trackStandbyActivity(handler, {}), handler);
});
