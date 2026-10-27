// OpenClaw 2026.9.9 drops DMs when a WhatsApp LID has no phone mapping.
// Keep transport identities usable, and retain verified mappings outside authDir
// so a QR relink cannot erase previously learned customer identities.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const LID = /^(\d+)(?::\d+)?@(lid|hosted\.lid)$/;
const PN = /^(\d{8,15})(?::\d+)?@(s\.whatsapp\.net|hosted)$/;
const DIGITS = /^\d{8,15}$/;

export function createRecovery({ root = path.join(os.homedir(), '.openclaw'), warn = console.warn } = {}) {
  const archive = path.join(root, 'runtime', 'whatsapp-identities', 'mappings.json');
  const identities = new Map();
  const conflicts = new Set();
  const opaque = new Set();
  const warned = new Set();
  let initialized = false;
  let dirty = false;
  const read = (file) => { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; } };
  function remember(lid, phone) {
    if (typeof lid !== 'string' || typeof phone !== 'string' || !/^\d+$/.test(lid) || !DIGITS.test(phone) || conflicts.has(lid)) return;
    if (identities.has(lid) && identities.get(lid) !== phone) {
      identities.delete(lid);
      conflicts.add(lid);
      dirty = true;
      warn('[whatsapp-lid-recovery] conflicting identity mappings; preserving opaque sender identity');
    } else if (!identities.has(lid)) { identities.set(lid, phone); dirty = true; }
  }
  function flush() {
    if (!dirty) return;
    try {
      fs.mkdirSync(path.dirname(archive), { recursive: true, mode: 0o700 });
      const tmp = `${archive}.${process.pid}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify({ version: 1, mappings: Object.fromEntries(identities), conflicts: [...conflicts], opaque: [...opaque] }), { mode: 0o600 });
      fs.renameSync(tmp, archive);
      dirty = false;
    } catch (error) { warn(`[whatsapp-lid-recovery] could not persist identity archive: ${error.code ?? 'write failed'}`); }
  }
  function accountDirs(cfg = {}) {
    const wa = cfg.channels?.whatsapp ?? {};
    const accounts = wa.accounts ?? { default: {} };
    return Object.entries(accounts).map(([id, value]) => {
      const custom = value?.authDir ?? (id === 'default' ? wa.authDir : undefined);
      return custom ? path.resolve(custom.replace(/^~(?=\/)/, os.homedir())) : path.join(root, 'credentials', 'whatsapp', id);
    });
  }
  function initialize(cfg) {
    if (initialized) return;
    initialized = true;
    const saved = read(archive);
    for (const lid of saved?.conflicts ?? []) conflicts.add(lid);
    for (const id of saved?.opaque ?? []) opaque.add(id);
    for (const [lid, phone] of Object.entries(saved?.mappings ?? {})) remember(lid, phone);
    for (const dir of accountDirs(cfg)) {
      let names;
      try { names = fs.readdirSync(dir); } catch { continue; }
      // Only reciprocal pairs are shared. Never copy Signal sessions or keys.
      for (const name of names) {
        const lid = /^lid-mapping-(\d+)_reverse\.json$/.exec(name)?.[1];
        if (!lid) continue;
        const phone = read(path.join(dir, name));
        if (typeof phone === 'string' && read(path.join(dir, `lid-mapping-${phone}.json`)) === lid) remember(lid, phone);
      }
      // Our own line identities must still reach the sibling-line loop guard.
      const me = read(path.join(dir, 'creds.json'))?.me;
      const lid = LID.exec(me?.lid ?? '')?.[1];
      const phone = PN.exec(me?.id ?? '')?.[1];
      if (lid && phone) remember(lid, phone);
    }
    flush();
  }
  return async function resolve(msg, { resolveInboundJid, cfg, accountId }) {
    initialize(cfg);
    const jid = msg.key?.remoteJid ?? '';
    const match = LID.exec(jid);
    if (!match) return resolveInboundJid(jid);
    const lid = match[1];
    const opaqueKey = `${accountId}:${match[2]}:${lid}`;
    // This alternate comes from the authenticated WhatsApp message key, never
    // customer text or a model-supplied phone number.
    const alternate = PN.exec(msg.key?.remoteJidAlt ?? '')?.[1];
    if (alternate) remember(lid, alternate);
    // Look up newly synced pairs too, without rescanning every credential file.
    for (const dir of accountDirs(cfg)) {
      const phone = read(path.join(dir, `lid-mapping-${lid}_reverse.json`));
      if (typeof phone === 'string' && read(path.join(dir, `lid-mapping-${phone}.json`)) === lid) remember(lid, phone);
    }
    if (!conflicts.has(lid)) {
      const resolved = await resolveInboundJid(jid);
      if (typeof resolved === 'string' && /^\+\d{8,15}$/.test(resolved)) remember(lid, resolved.slice(1));
    }
    if (identities.has(lid) && !conflicts.has(lid) && !opaque.has(opaqueKey)) {
      flush();
      return `+${identities.get(lid)}`;
    }
    // Do not split a conversation (or lose its handoff/CV state) if WhatsApp
    // exposes a phone later. Once used, an opaque peer stays stable per account.
    if (!opaque.has(opaqueKey)) { opaque.add(opaqueKey); dirty = true; }
    flush();
    if (!warned.has(lid)) {
      warned.add(lid);
      if (warned.size > 1000) warned.delete(warned.values().next().value);
      warn(`[whatsapp-lid-recovery] accepting DM with opaque WhatsApp identity (account=${accountId}); phone mapping unavailable`);
    }
    // An LID is an addressable WhatsApp identity, not an E.164 phone number.
    return `${lid}@${match[2]}`;
  };
}

export const resolveWhatsAppSender = createRecovery();
