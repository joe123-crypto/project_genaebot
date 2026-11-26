// Offline checks against the pinned SDK used by WhatsApp's inbound admission.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import test from 'node:test';

const root = process.env.GENAIE_OPENCLAW_ROOT;
if (process.env.GENAIE_REQUIRE_INTEGRATION === '1' && !root) {
  throw new Error('required installed-runtime integration cannot skip');
}

const TEMPLATE = fileURLToPath(new URL('../../config/openclaw.template.json', import.meta.url));
const SYNC_REPLY_GATES = fileURLToPath(new URL('./sync-reply-gates.py', import.meta.url));

// The template seeds no WhatsApp line: a workspace makes its own from the
// console. This install's two lines live only in its live config, and the
// boot-time gate sync forces the policy each must carry (ACCOUNT_GATES in
// sync-reply-gates.py). So build the config the way a boot does -- the
// template, the two lines bare, then the real gate sync over them -- and check
// what comes out against the pinned SDK.
function syncedLiveConfig() {
  const live = JSON.parse(fs.readFileSync(TEMPLATE, 'utf8'));
  // Production adds its operator number and wildcard to this preserved list.
  live.channels.whatsapp.allowFrom = ['+19995550100', '*'];
  // ads.enabled is operator-controlled; the gate sync never writes it.
  live.channels.whatsapp.accounts = { default: {}, ads: { enabled: true } };
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dm-policy-'));
  try {
    const file = path.join(dir, 'openclaw.json');
    fs.writeFileSync(file, JSON.stringify(live));
    const env = { ...process.env, PYTHONDONTWRITEBYTECODE: '1' };
    delete env.REPLIES_DISABLED;
    const run = spawnSync('python3', [SYNC_REPLY_GATES, TEMPLATE, file], { encoding: 'utf8', env });
    assert.equal(run.status, 0, run.stderr);
    // The gate sync is never fatal: a failure shows only on stderr.
    assert.equal(run.stderr, '');
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test('native ingress blocks every ads DM while leaving default DMs open',
  { skip: !root }, async () => {
    const { resolveStableChannelMessageIngress } = await import(pathToFileURL(
      path.join(root, 'dist/plugin-sdk/channel-ingress-runtime.js')));
    const cfg = syncedLiveConfig();
    const receive = async (accountId, senderId, { group = false, config = cfg } = {}) => {
      const channel = config.channels.whatsapp;
      const account = { ...channel, ...channel.accounts[accountId] };
      return resolveStableChannelMessageIngress({
        channelId: 'whatsapp', accountId, cfg: config,
        identity: {
          key: 'whatsapp-sender-phone', kind: 'phone', sensitivity: 'pii',
          entryIdPrefix: 'whatsapp-entry', normalize: value => value.trim(),
        },
        subject: { stableId: senderId },
        conversation: { kind: group ? 'group' : 'direct', id: group ? '1@g.us' : senderId },
        dmPolicy: account.dmPolicy, groupPolicy: account.groupPolicy,
        policy: { groupAllowFromFallbackToAllowFrom: false },
        allowFrom: account.allowFrom, groupAllowFrom: [],
      });
    };
    // Customers, operator/default sibling, and the ads account's own phone all
    // remain blocked even though the inherited allowlist contains "*".
    for (const sender of ['+19995550123', '+19995550100', '+19995550101', '19995550102@lid']) {
      const blocked = await receive('ads', sender);
      assert.equal(blocked.senderAccess.decision, 'block');
      assert.equal(blocked.senderAccess.reasonCode, 'dm_policy_disabled');
      assert.equal((await receive('default', sender)).senderAccess.decision, 'allow');
    }
    // The sync seeds the default line's listener and leaves the ads listener on,
    // so scheduled group advertising continues.
    assert.equal(cfg.channels.whatsapp.accounts.default.enabled, true);
    assert.equal(cfg.channels.whatsapp.accounts.ads.enabled, true);
    for (const account of ['default', 'ads']) {
      assert.equal((await receive(account, '+19995550123', { group: true }))
        .senderAccess.reasonCode, 'group_policy_disabled');
    }
    const resumed = structuredClone(cfg);
    resumed.channels.whatsapp.accounts.ads.dmPolicy = 'open';
    assert.equal((await receive('ads', '+19995550123', { config: resumed }))
      .senderAccess.decision, 'allow');
  });
