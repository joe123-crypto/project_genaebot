// Offline checks against the pinned SDK used by WhatsApp's inbound admission.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';

const root = process.env.GENAIE_OPENCLAW_ROOT;
if (process.env.GENAIE_REQUIRE_INTEGRATION === '1' && !root) {
  throw new Error('required installed-runtime integration cannot skip');
}

test('native ingress blocks every ads DM while leaving default DMs open',
  { skip: !root }, async () => {
    const { resolveStableChannelMessageIngress } = await import(pathToFileURL(
      path.join(root, 'dist/plugin-sdk/channel-ingress-runtime.js')));
    const cfg = JSON.parse(fs.readFileSync(new URL('../../config/openclaw.template.json', import.meta.url)));
    // Production adds its operator number and wildcard to this preserved list.
    cfg.channels.whatsapp.allowFrom = ['+19995550100', '*'];
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
