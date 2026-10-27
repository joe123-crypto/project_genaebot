// Load-time compatibility patch: covers installed packages and OpenClaw's
// integrity-checked plugin captures without changing vendor files on disk.
import { registerHooks } from 'node:module';

const HELPER = new URL('./whatsapp-lid-recovery.mjs', import.meta.url).href;
const FROM = 'const from = group ? remoteJid : await socketSession.resolveInboundJid(remoteJid);';
const SENDER = 'const senderE164 = group ? participantJid ? await socketSession.resolveInboundJid(participantJid) : null : from;';
const PHONE_NORMALIZER = 'function normalizeWhatsAppIngressPhone(value) {\n\tconst trimmed = value.trim();\n\tif (!trimmed) return null;\n\treturn normalizeE164(trimmed);\n}';
const MARKER = '// GENAIE_WHATSAPP_LID_RECOVERY_V1';

export function patchWhatsAppMonitor(source) {
  if (source.includes(MARKER)) return source;
  if (!source.includes('function createWhatsAppInboundMessageNormalizer(')) return source;
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
\t\treturn { jid: null, lid: conversation.id, e164: null, name: msg.platform.senderName ?? null };
\t}
`);
}

registerHooks({
  load(url, context, nextLoad) {
    const result = nextLoad(url, context);
    if (result.format !== 'module' || !url.includes('/@openclaw/whatsapp/')) return result;
    const patch = /\/monitor-[^/]+\.mjs$/.test(url) ? patchWhatsAppMonitor : /\/auth-store-[^/]+\.mjs$/.test(url) ? patchWhatsAppIdentity : null;
    if (!patch) return result;
    const source = typeof result.source === 'string' ? result.source : new TextDecoder().decode(result.source);
    return { ...result, source: patch(source) };
  },
});
