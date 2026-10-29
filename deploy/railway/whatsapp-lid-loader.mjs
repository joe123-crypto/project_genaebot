// Load-time compatibility patch: covers installed packages and OpenClaw's
// integrity-checked plugin captures without changing vendor files on disk.
import { registerHooks } from 'node:module';
import { readFileSync } from 'node:fs';

export const SUPPORTED_WHATSAPP_VERSION = '2026.9.9';
const checkedPackages = new Set();
const HELPER = new URL('./whatsapp-lid-recovery.mjs', import.meta.url).href;
const CACHE_HELPER = new URL('./whatsapp-cache-cleanup.mjs', import.meta.url).href;
const CACHE_MARKER = '// GENAIE_WHATSAPP_EXPIRED_CACHE_CLEANUP_V1';
const CACHE_START = '\t\tif (started || closed) return;\n\t\tstarted = true;\n\t\tconst listen = (event, listener) => {';
const FROM = 'const from = group ? remoteJid : await socketSession.resolveInboundJid(remoteJid);';
const SENDER = 'const senderE164 = group ? participantJid ? await socketSession.resolveInboundJid(participantJid) : null : from;';
const PHONE_NORMALIZER = 'function normalizeWhatsAppIngressPhone(value) {\n\tconst trimmed = value.trim();\n\tif (!trimmed) return null;\n\treturn normalizeE164(trimmed);\n}';
const MARKER = '// GENAIE_WHATSAPP_LID_RECOVERY_V1';

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
    if (patch === patchWhatsAppMonitor && process.env.GENAIE_WHATSAPP_CACHE_CLEANUP === '1') patched = patchWhatsAppCacheCleanup(patched);
    return { ...result, source: patched };
  },
});
