// Load-time compatibility patch: covers installed packages and OpenClaw's
// integrity-checked plugin captures without changing vendor files on disk.
import { registerHooks } from 'node:module';
import { readFileSync } from 'node:fs';

export const SUPPORTED_WHATSAPP_VERSION = '2026.9.9';
const checkedPackages = new Set();
const HELPER = new URL('./whatsapp-lid-recovery.mjs', import.meta.url).href;
const CACHE_HELPER = new URL('./whatsapp-cache-cleanup.mjs', import.meta.url).href;
const HEALTH_HELPER = new URL('./whatsapp-health-audit.mjs', import.meta.url).href;
const DISCOVERY_HELPER = new URL('./whatsapp-group-discovery.mjs', import.meta.url).href;
const ACTIVITY_HELPER = new URL('./whatsapp-group-activity.mjs', import.meta.url).href;
const ACTIONS_HELPER = new URL('./whatsapp-group-actions.mjs', import.meta.url).href;
const TAKEOVER_HELPER = new URL('./whatsapp-human-takeover.mjs', import.meta.url).href;
const CACHE_MARKER = '// GENAIE_WHATSAPP_EXPIRED_CACHE_CLEANUP_V1';
const CACHE_START = '\t\tif (started || closed) return;\n\t\tstarted = true;\n\t\tconst listen = (event, listener) => {';
const FROM = 'const from = group ? remoteJid : await socketSession.resolveInboundJid(remoteJid);';
const SENDER = 'const senderE164 = group ? participantJid ? await socketSession.resolveInboundJid(participantJid) : null : from;';
const PHONE_NORMALIZER = 'function normalizeWhatsAppIngressPhone(value) {\n\tconst trimmed = value.trim();\n\tif (!trimmed) return null;\n\treturn normalizeE164(trimmed);\n}';
const MARKER = '// GENAIE_WHATSAPP_LID_RECOVERY_V1';

export function patchWhatsAppGroupDiscovery(source) {
  const marker = '// GENAIE_WHATSAPP_GROUP_DISCOVERY_V1';
  if (source.includes(marker)) return source;
  const changes = [
    ['\tconst controller = new WhatsAppConnectionController({',
      '\tconst genaieGroupDiscovery = genaieCreateWhatsAppGroupDiscovery({ accountId: account.accountId });\n', false],
    ['\t\t\tstatusController.noteConnected();',
      '\n\t\t\tgenaieGroupDiscovery.start(controller.getCurrentSock());', true],
    ['\t\t\tconst reason = await controller.waitForClose().finally(() => {',
      '\n\t\t\t\tgenaieGroupDiscovery.stop();', true],
    ['\t\tstatusController.markStopped();',
      '\t\tgenaieGroupDiscovery.stop();\n', false],
  ];
  for (const [anchor] of changes) {
    if (source.split(anchor).length !== 2) throw new Error('WhatsApp group discovery contract changed; review adapter before upgrading');
  }
  for (const [anchor, addition, after] of changes) source = source.replace(anchor, after ? anchor + addition : addition + anchor);
  return `${marker}\nimport { createWhatsAppGroupDiscovery as genaieCreateWhatsAppGroupDiscovery } from ${JSON.stringify(DISCOVERY_HELPER)};\n` + source;
}

export function patchWhatsAppGroupActivity(source) {
  const marker = '// GENAIE_WHATSAPP_GROUP_ACTIVITY_V1';
  if (source.includes(marker)) return source;
  const changes = [
    ['\tconst controller = new WhatsAppConnectionController({',
      '\tconst genaieGroupActivity = genaieCreateWhatsAppGroupActivity({ accountId: account.accountId });\n', false],
    ['\t\t\t\t\tcreateListener: async ({ sock, connection: connectionLocal }) => {',
      '\n\t\t\t\t\t\tgenaieGroupActivity.start(sock);', true],
    ['\t\t\tconst reason = await controller.waitForClose().finally(() => {',
      '\n\t\t\t\tgenaieGroupActivity.stop();', true],
    ['\t\tstatusController.markStopped();', '\t\tgenaieGroupActivity.stop();\n', false],
  ];
  for (const [anchor] of changes) {
    if (source.split(anchor).length !== 2) throw new Error('WhatsApp activity contract changed; review adapter before upgrading');
  }
  for (const [anchor, addition, after] of changes) source = source.replace(anchor, after ? anchor + addition : addition + anchor);
  return `${marker}\nimport { createWhatsAppGroupActivity as genaieCreateWhatsAppGroupActivity } from ${JSON.stringify(ACTIVITY_HELPER)};\n` + source;
}

export function patchWhatsAppGroupActions(source) {
  const marker = '// GENAIE_WHATSAPP_GROUP_ACTIONS_V1';
  if (source.includes(marker)) return source;
  const changes = [
    ['\tconst controller = new WhatsAppConnectionController({',
      '\tconst genaieGroupActions = genaieCreateWhatsAppGroupActions({ accountId: account.accountId });\n', false],
    ['\t\t\tstatusController.noteConnected();',
      '\n\t\t\tgenaieGroupActions.start(controller.getCurrentSock());', true],
    ['\t\t\tconst reason = await controller.waitForClose().finally(() => {',
      '\n\t\t\t\tgenaieGroupActions.stop();', true],
    ['\t\tstatusController.markStopped();',
      '\t\tgenaieGroupActions.stop();\n', false],
  ];
  for (const [anchor] of changes) {
    if (source.split(anchor).length !== 2) throw new Error('WhatsApp group actions contract changed; review adapter before upgrading');
  }
  for (const [anchor, addition, after] of changes) source = source.replace(anchor, after ? anchor + addition : addition + anchor);
  return `${marker}\nimport { createWhatsAppGroupActions as genaieCreateWhatsAppGroupActions } from ${JSON.stringify(ACTIONS_HELPER)};\n` + source;
}

// Inbound access control drops a DM the operator typed by hand (it is fromMe,
// and the chat is not the operator's self-chat) without telling anyone. Record
// the peer first, so the public-service-gate plugin can stop replying there. By
// this point the adapter has already dropped the echoes of the bot's own sends
// and every receipt, reaction and deletion, so what reaches here is a person.
export function patchWhatsAppHumanTakeover(source) {
  const marker = '// GENAIE_WHATSAPP_HUMAN_TAKEOVER_V1';
  if (source.includes(marker)) return source;
  const guard = '\t\tif (params.isFromMe && (policy.account.selfChatMode === false || !policy.isSamePhone(params.from))) {';
  const anchor = '\t\t\tlogWhatsAppVerbose$2(params.verbose, "Skipping outbound DM (fromMe); no pairing reply needed.");\n\t\t\treturn blockedInboundAccess(policy);';
  for (const contract of [guard, anchor]) {
    if (source.split(contract).length !== 2) throw new Error('WhatsApp human takeover contract changed; review adapter before upgrading');
  }
  // The echo filter and the user-content filter are what make this branch mean
  // "a person typed this". Without them in front of it, it means nothing.
  for (const ordering of ['if (shouldSkipRecentOutboundEcho(msg)) return null;', 'if (!hasInboundUserContent(msg.message ?? void 0)) return null;']) {
    if (source.split(ordering).length !== 2) throw new Error('WhatsApp outbound echo filtering contract changed; review adapter before upgrading');
  }
  const addition = '\n\t\t\tgenaieNoteHumanTakeover({ accountId: params.accountId, from: params.from, remoteJid: params.remoteJid, group: params.group, selfE164: params.selfE164, messageTimestampMs: params.messageTimestampMs, connectedAtMs: params.connectedAtMs });';
  const replacement = anchor.replace('\n\t\t\treturn blockedInboundAccess(policy);', addition + '\n\t\t\treturn blockedInboundAccess(policy);');
  return `${marker}\nimport { noteHumanTakeover as genaieNoteHumanTakeover } from ${JSON.stringify(TAKEOVER_HELPER)};\n` + source.replace(anchor, () => replacement);
}

export function patchWhatsAppHealthAudit(source) {
  const marker = '// GENAIE_WHATSAPP_HEALTH_AUDIT_V1';
  if (source.includes(marker)) return source;
  const changes = [
    ['\tconst controller = new WhatsAppConnectionController({',
      '\tconst genaieAudit = genaieCreateWhatsAppHealthAudit({ accountId: account.accountId, runId });\n'],
    ['\t\t\tconst connectionId = newConnectionId();\n\t\t\tconst shouldDebounce',
      '\t\t\tgenaieAudit({ event: "connect_attempt", connectionId });\n', true],
    ['\t\t\t\t\t\tconst watchdogReason = transportSilentMs > transportTimeoutMs ? "transport-inactive" : "app-silent";',
      '\n\t\t\t\t\t\tgenaieAudit({ event: "watchdog", connectionId: snapshot.connectionId, reason: watchdogReason });', true],
    ['\t\t\t\tif (setupDecision) {',
      '\n\t\t\t\t\tgenaieAudit({ event: "setup_failure", connectionId, statusCode: setupDecision.normalized.statusCode, reason: setupDecision.healthState });', true],
    ['\t\t\tstatusController.noteConnected();',
      '\n\t\t\tgenaieAudit({ event: "connected", connectionId, reason: "connected" });', true],
    ['\t\t\tstatusController.noteReconnectAttempts(controller.getReconnectAttempts());',
      '\t\t\tgenaieAudit({ event: "disconnected", connectionId, statusCode: decision.normalized.statusCode, reason: decision.healthState });\n'],
    ['\t\tstatusController.markStopped();',
      '\t\tgenaieAudit({ event: "stopped", reason: stopRequested() || sigintStop ? "shutdown" : "stopped", planned: stopRequested() || sigintStop });\n'],
  ];
  for (const [anchor, addition, after] of changes) {
    if (source.split(anchor).length !== 2) throw new Error('WhatsApp lifecycle audit contract changed; review adapter before upgrading');
    // The connection ID anchor spans the next declaration; insert between them.
    const replacement = anchor.includes('const shouldDebounce')
      ? anchor.replace('\n\t\t\tconst shouldDebounce', '\n' + addition + '\t\t\tconst shouldDebounce')
      : after ? anchor + addition : addition + anchor;
    source = source.replace(anchor, replacement);
  }
  return `${marker}\nimport { createWhatsAppHealthAudit as genaieCreateWhatsAppHealthAudit } from ${JSON.stringify(HEALTH_HELPER)};\n` + source;
}

export function patchWhatsAppCacheCleanup(source) {
  if (source.includes(CACHE_MARKER)) return source;
  for (const contract of [
    'function createWhatsAppGroupMetadataCacheOwner(params)',
    'const localCache = /* @__PURE__ */ new Map();',
    'if (entry.expiresAt <= Date.now())',
    'expires <= now',
    'for (const detach of detachListeners.splice(0)) detach();',
  ]) {
    if (!source.includes(contract)) throw new Error('WhatsApp cache cleanup contract changed; review before enabling');
  }
  if (source.split(CACHE_START).length !== 2) throw new Error('WhatsApp cache cleanup owner start changed; review before enabling');
  const replacement = CACHE_START.replace('\t\tconst listen', `\t\tdetachListeners.push(genaieStartExpiredCacheCleanup([\n\t\t\t[localCache, "expires"],\n\t\t\t[reconnectCache, "expires"],\n\t\t\t[params.baileysCache, "expiresAt"]\n\t\t]));\n\t\tconst listen`);
  return `${CACHE_MARKER}\nimport { startExpiredWhatsAppCacheCleanup as genaieStartExpiredCacheCleanup } from ${JSON.stringify(CACHE_HELPER)};\n` + source.replace(CACHE_START, replacement);
}

export function patchWhatsAppMonitor(source) {
  if (source.includes(MARKER)) return source;
  if (!source.includes('function createWhatsAppInboundMessageNormalizer(')) throw new Error('WhatsApp LID compatibility patch no longer matches upstream; review before upgrading OpenClaw');
  for (const anchor of [FROM, SENDER, PHONE_NORMALIZER]) {
    if (source.split(anchor).length !== 2) throw new Error('WhatsApp LID compatibility patch no longer matches upstream; review before upgrading OpenClaw');
  }
  return `${MARKER}\nimport { resolveWhatsAppSender as genaieResolveWhatsAppSender } from ${JSON.stringify(HELPER)};\n` + source
    .replace(PHONE_NORMALIZER, PHONE_NORMALIZER.replace('return normalizeE164(trimmed);', 'const lid = /^(\\d+)(?::\\d+)?@(lid|hosted\\.lid)$/.exec(trimmed);\n\tif (lid) return `${lid[1]}@${lid[2]}`;\n\treturn normalizeE164(trimmed);'))
    .replace(FROM, 'const from = group ? remoteJid : await genaieResolveWhatsAppSender(msg, { resolveInboundJid: socketSession.resolveInboundJid, cfg: options.loadConfig?.() ?? options.cfg, accountId: options.accountId });')
    .replace(SENDER, 'const senderE164 = group ? participantJid ? await socketSession.resolveInboundJid(participantJid) : null : from?.includes("@") ? null : from;');
}

export function patchWhatsAppIdentity(source) {
  if (source.includes(MARKER)) return source;
  const anchor = 'function getSenderIdentity(msg, authDir) {';
  if (source.split(anchor).length !== 2) throw new Error('WhatsApp LID identity patch no longer matches upstream; review before upgrading OpenClaw');
  return `${MARKER}\n` + source.replace(anchor, anchor + `
\tconst conversation = msg.admission?.conversation;
\tif (conversation?.kind === "direct" && /^\\d+@(lid|hosted\\.lid)$/.test(conversation.id)) {
\t\treturn { jid: null, lid: conversation.id, e164: null, name: msg.platform.senderName ?? msg.platform.sender?.name ?? null };
\t}
`);
}

export function patchWhatsAppTargets(source) {
  if (source.includes(MARKER)) return source;
  const anchor = 'function normalizeWhatsAppTarget(value) {\n\tconst candidate = stripWhatsAppTargetPrefixes(value);';
  if (source.split(anchor).length !== 2) throw new Error('WhatsApp LID target patch no longer matches upstream; review before upgrading OpenClaw');
  return `${MARKER}\n` + source.replace(anchor, anchor + `
\tconst opaque = /^(\\d{1,32})(?::\\d+)?@(lid|hosted\\.lid)$/i.exec(candidate);
\tif (opaque) return \`\${opaque[1]}@\${opaque[2].toLowerCase()}\`;
`);
}

registerHooks({
  load(url, context, nextLoad) {
    const result = nextLoad(url, context);
    if (result.format !== 'module' || !url.includes('/@openclaw/whatsapp/')) return result;
    const packageUrl = url.slice(0, url.indexOf('/@openclaw/whatsapp/') + '/@openclaw/whatsapp/'.length);
    if (!checkedPackages.has(packageUrl)) {
      const version = JSON.parse(readFileSync(new URL('package.json', packageUrl), 'utf8')).version;
      if (version !== SUPPORTED_WHATSAPP_VERSION) throw new Error(`WhatsApp compatibility code requires adapter ${SUPPORTED_WHATSAPP_VERSION}; review the patch before upgrading`);
      checkedPackages.add(packageUrl);
    }
    const patch = /\/monitor-[^/]+\.mjs$/.test(url) ? patchWhatsAppMonitor : /\/auth-store-[^/]+\.mjs$/.test(url) ? patchWhatsAppIdentity : /\/normalize-target-[^/]+\.mjs$/.test(url) ? patchWhatsAppTargets : null;
    if (!patch) return result;
    const source = typeof result.source === 'string' ? result.source : new TextDecoder().decode(result.source);
    let patched = patch(source);
    if (patch === patchWhatsAppMonitor) patched = patchWhatsAppHealthAudit(patched);
    if (patch === patchWhatsAppMonitor && process.env.GENAIE_GROUP_ACTIVITY !== '0') {
      try { patched = patchWhatsAppGroupActivity(patched); }
      catch { console.warn('[whatsapp-activity] collection integration unavailable; review adapter compatibility'); }
    }
    if (patch === patchWhatsAppMonitor && process.env.GENAIE_GROUP_DISCOVERY !== '0') {
      try { patched = patchWhatsAppGroupDiscovery(patched); }
      catch { console.warn('[whatsapp-groups] discovery integration unavailable; review adapter compatibility'); }
    }
    if (patch === patchWhatsAppMonitor && process.env.GENAIE_GROUP_ACTIONS !== '0') {
      try { patched = patchWhatsAppGroupActions(patched); }
      catch { console.warn('[whatsapp-group-actions] group actions unavailable; review adapter compatibility'); }
    }
    if (patch === patchWhatsAppMonitor && process.env.GENAIE_HUMAN_TAKEOVER !== '0') {
      try { patched = patchWhatsAppHumanTakeover(patched); }
      catch { console.warn('[whatsapp-takeover] detection unavailable; review adapter compatibility'); }
    }
    if (patch === patchWhatsAppMonitor && process.env.GENAIE_WHATSAPP_CACHE_CLEANUP === '1') patched = patchWhatsAppCacheCleanup(patched);
    return { ...result, source: patched };
  },
});
