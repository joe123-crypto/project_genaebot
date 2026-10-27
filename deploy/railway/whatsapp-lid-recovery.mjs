// Preserve addressable WhatsApp identities without treating an archived phone
// alias as fresh authentication. This runs under the gateway's single owner.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const LID = /^(\d{1,32})(?::\d+)?@(lid|hosted\.lid)$/;
const PN = /^(\d{8,15})(?::\d+)?@(s\.whatsapp\.net|hosted)$/;
const DIGITS = /^\d{8,15}$/;
const phoneDigits = (value) => typeof value === 'string' && /^\+?\d{8,15}$/.test(value.trim()) ? value.trim().replace(/^\+/, '') : null;
function configuredPhone(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim().replace(/^whatsapp:/i, '');
  const pn = PN.exec(text);
  if (pn) return pn[1];
  if (!/^[+()\d\s-]+$/.test(text)) return null;
  const digits = text.replace(/\D/g, '');
  return DIGITS.test(digits) ? digits : null;
}
const list = (value) => Array.isArray(value) ? value : [];
const keyFor = (account, domain, lid) => JSON.stringify([account, domain, lid]);
function parseKey(key) {
  try {
    const parts = JSON.parse(key);
    if (Array.isArray(parts) && parts.length === 3 && parts.every(value => typeof value === 'string') && /^(lid|hosted\.lid)$/.test(parts[1]) && /^\d{1,32}$/.test(parts[2])) return parts;
  } catch {}
  return null;
}

export function createRecovery({ root = path.join(os.homedir(), '.openclaw'), warn = console.warn, lookupTimeoutMs = 1500 } = {}) {
  const archive = path.join(root, 'runtime', 'whatsapp-identities', 'mappings.json');
  const mappings = new Map(), conflicts = new Set(), opaque = new Set(), warned = new Set();
  let initialized = false, dirty = false;
  const read = (file) => { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; } };
  const warnOnce = (key, text) => {
    if (warned.has(key)) return;
    warned.add(key);
    if (warned.size > 1000) warned.delete(warned.values().next().value);
    warn(`[whatsapp-lid-recovery] ${text}`);
  };
  function remember(key, phone) {
    if (!DIGITS.test(phone) || conflicts.has(key)) return;
    if (mappings.has(key) && mappings.get(key) !== phone) {
      mappings.delete(key); conflicts.add(key); dirty = true;
      warnOnce(`conflict:${key}`, 'conflicting identity mappings; preserving opaque sender identity');
    } else if (!mappings.has(key)) { mappings.set(key, phone); dirty = true; }
  }
  function flush() {
    if (!dirty) return;
    try {
      fs.mkdirSync(path.dirname(archive), { recursive: true, mode: 0o700 });
      const tmp = `${archive}.${process.pid}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify({ version: 2, mappings: Object.fromEntries(mappings), conflicts: [...conflicts], opaque: [...opaque] }), { mode: 0o600 });
      fs.renameSync(tmp, archive); dirty = false;
    } catch (error) { warnOnce('archive-write', `could not persist identity archive: ${error.code ?? 'write failed'}`); }
  }
  function initialize() {
    if (initialized) return;
    initialized = true;
    const saved = read(archive);
    if (!saved) return;
    if (saved.version !== 2) {
      // Version 1 mixed accounts/domains. Retain only its explicitly scoped
      // opaque peers; its unscoped phone aliases cannot establish an identity.
      if (Array.isArray(saved.opaque)) for (const value of saved.opaque) {
        const match = typeof value === 'string' && /^([^:]+):(lid|hosted\.lid):(\d{1,32})$/.exec(value);
        if (match) opaque.add(keyFor(match[1], match[2], match[3]));
      }
      dirty = true;
      warnOnce('archive-version', 'discarded unscoped legacy phone aliases; using current account evidence');
      return;
    }
    for (const name of ['conflicts', 'opaque']) {
      if (!Array.isArray(saved[name])) continue;
      for (const key of saved[name]) if (typeof key === 'string' && parseKey(key)) (name === 'conflicts' ? conflicts : opaque).add(key);
    }
    if (saved.mappings && typeof saved.mappings === 'object' && !Array.isArray(saved.mappings)) {
      for (const [key, phone] of Object.entries(saved.mappings)) if (parseKey(key) && typeof phone === 'string') remember(key, phone);
    }
  }
  function accountDir(cfg, accountId) {
    const wa = cfg?.channels?.whatsapp ?? {};
    const custom = wa.accounts?.[accountId]?.authDir ?? (accountId === 'default' ? wa.authDir : undefined);
    return typeof custom === 'string' && custom.trim() ? path.resolve(custom.replace(/^~(?=\/)/, os.homedir())) : path.join(root, 'credentials', 'whatsapp', accountId);
  }
  function localPair(cfg, account, lid, domain) {
    // Legacy mapping filenames have no domain, so they cannot prove a hosted ID.
    if (domain !== 'lid') return null;
    const dir = accountDir(cfg, account), phone = read(path.join(dir, `lid-mapping-${lid}_reverse.json`));
    return typeof phone === 'string' && DIGITS.test(phone) && read(path.join(dir, `lid-mapping-${phone}.json`)) === lid ? phone : null;
  }
  function ownLine(cfg, lid, domain) {
    const phones = new Set();
    for (const account of Object.keys(cfg?.channels?.whatsapp?.accounts ?? { default: {} })) {
      const me = read(path.join(accountDir(cfg, account), 'creds.json'))?.me;
      const id = LID.exec(me?.lid ?? ''), pn = PN.exec(me?.id ?? '');
      if (id?.[1] === lid && id[2] === domain && pn) phones.add(pn[1]);
    }
    if (phones.size > 1) throw new Error('Conflicting local WhatsApp line identities; refusing ambiguous sender');
    return [...phones][0] ?? null;
  }
  function archiveMayIdentify(cfg, account, phone) {
    const wa = cfg?.channels?.whatsapp ?? {}, defaults = wa.accounts?.default ?? {}, current = wa.accounts?.[account] ?? {};
    if ((current.dmPolicy ?? defaults.dmPolicy ?? wa.dmPolicy ?? 'pairing') !== 'open') return false;
    const gate = cfg?.plugins?.entries?.['public-service-gate']?.config ?? {};
    const privileged = [
      ...list(gate.whatsappAdminUsers), ...list(gate.publicUsers), ...Object.values(gate.whatsappLines ?? {}),
      ...list(wa.allowFrom), ...list(defaults.allowFrom), ...list(current.allowFrom),
      ...list(cfg?.commands?.ownerAllowFrom), ...list(cfg?.commands?.allowFrom?.whatsapp), ...list(cfg?.commands?.allowFrom?.['*']),
      ...list(cfg?.tools?.elevated?.allowFrom?.whatsapp),
      ...list(cfg?.bindings).filter(b => b.agentId !== 'public' && b.match?.peer?.kind === 'direct').map(b => b.match.peer.id),
    ];
    return !privileged.some(value => configuredPhone(value) === phone);
  }
  async function lookup(jid, resolver) {
    let timer;
    try {
      return await Promise.race([
        Promise.resolve().then(() => resolver(jid)),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('lookup timed out')), lookupTimeoutMs); }),
      ]);
    } catch {
      warnOnce('lookup-failed', 'phone lookup failed or timed out; retaining the addressable WhatsApp ID');
      return null;
    } finally { clearTimeout(timer); }
  }
  return async function resolve(msg, { resolveInboundJid, cfg = {}, accountId = 'default' }) {
    initialize();
    const jid = msg.key?.remoteJid ?? '', lid = LID.exec(jid), pn = PN.exec(jid);
    if (!lid && !pn) return null;
    if (pn) {
      const domain = pn[2] === 'hosted' ? 'hosted.lid' : 'lid', phone = pn[1];
      const alternate = LID.exec(msg.key?.remoteJidAlt ?? '');
      let candidate = alternate?.[2] === domain ? alternate[1] : null;
      if (!candidate && domain === 'lid') {
        const dir = accountDir(cfg, accountId), value = read(path.join(dir, `lid-mapping-${phone}.json`));
        if (typeof value === 'string' && /^\d{1,32}$/.test(value) && localPair(cfg, accountId, value, domain) === phone) candidate = value;
      }
      if (!candidate) {
        const matches = [...mappings].filter(([key, value]) => {
          const parts = parseKey(key);
          return value === phone && parts?.[0] === accountId && parts[1] === domain && opaque.has(key) && !conflicts.has(key);
        });
        if (matches.length === 1) candidate = parseKey(matches[0][0])[2];
      }
      if (candidate) {
        const key = keyFor(accountId, domain, candidate); remember(key, phone); flush();
        if (opaque.has(key)) return `${candidate}@${domain}`;
      }
      return `+${phone}`;
    }
    const domain = lid[2], digits = lid[1], key = keyFor(accountId, domain, digits);
    // Local line credentials identify our own sibling line across accounts;
    // unrelated customer mappings are never copied between account stores.
    const own = ownLine(cfg, digits, domain);
    if (own) return `+${own}`;
    const alternate = PN.exec(msg.key?.remoteJidAlt ?? '');
    let current = alternate && (domain === 'hosted.lid' ? alternate[2] === 'hosted' : alternate[2] === 's.whatsapp.net') ? alternate[1] : null;
    current ??= localPair(cfg, accountId, digits, domain);
    if (!current && domain === 'lid') {
      const resolved = await lookup(jid, resolveInboundJid);
      current = phoneDigits(resolved);
    }
    if (current) remember(key, current);
    const phone = mappings.get(key);
    if (phone && !conflicts.has(key) && !opaque.has(key) && (current === phone || archiveMayIdentify(cfg, accountId, phone))) { flush(); return `+${phone}`; }
    if (!opaque.has(key)) { opaque.add(key); dirty = true; }
    flush();
    warnOnce(key, `accepting DM with a stable opaque WhatsApp identity (account=${accountId})`);
    return `${digits}@${domain}`;
  };
}

export const resolveWhatsAppSender = createRecovery();
