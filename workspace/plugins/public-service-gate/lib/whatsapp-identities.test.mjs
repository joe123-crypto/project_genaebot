import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { getEventSender, normalizePhone } from './events.js';
import { isWhatsAppAdmin, parseAllowCommand } from '../services/membership.js';
import { parseResumeCommand } from '../services/handoff.js';
import { lineKey } from './sibling-lines.js';
import { markFailsafeStopSuppression, hasFailsafeStopSuppression, clearFailsafeStopSuppression } from './failsafe.js';
import { markHandedOff, isHandedOff, clearHandoff } from './handoffs.js';

const LID = '263785632943@lid'; // Same digits as admin must NOT grant admin.
const PHONE = '+263785632943';

test('opaque direct identity stays distinct from an identically numbered phone', () => {
  assert.equal(getEventSender({ senderId: LID }), LID);
  assert.equal(normalizePhone('263785632943:7@lid'), LID);
  assert.equal(normalizePhone('1234:2@hosted.lid'), '1234@hosted.lid');
  assert.equal(normalizePhone('12036312345@g.us'), '');
  assert.equal(lineKey(LID), '');
  assert.equal(isWhatsAppAdmin({ config: { plugins: { entries: { 'public-service-gate': { config: { whatsappAdminUsers: [PHONE] } } } } } }, LID), false);
});
test('failsafe state for LID does not suppress a phone with the same digits', () => {
  clearFailsafeStopSuppression(PHONE); clearFailsafeStopSuppression(LID);
  markFailsafeStopSuppression(LID);
  assert.equal(hasFailsafeStopSuppression(LID), true);
  assert.equal(hasFailsafeStopSuppression(PHONE), false);
  clearFailsafeStopSuppression(LID);
});
test('handoff can be stored and resumed using an opaque sender identity', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lid-handoff-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const api = { pluginConfig: { serviceStatePath: path.join(root, 'state.json') } };
  markHandedOff(api, LID, 'other');
  assert.equal(isHandedOff(api, LID), true);
  assert.equal(isHandedOff(api, PHONE), false);
  assert.equal(parseResumeCommand(`resume ${LID}`), LID);
  assert.equal(parseAllowCommand(`allow ${LID}`), LID);
  assert.equal(clearHandoff(api, LID), true);
  assert.equal(isHandedOff(api, LID), false);
});
