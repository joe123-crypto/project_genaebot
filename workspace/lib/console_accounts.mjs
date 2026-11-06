// Gateway-side account controls. No credentials or raw gateway errors leave this bridge.
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { setTimeout as delay } from 'node:timers/promises';
import { loadCampaignGatewayClient } from './gateway_campaign.mjs';

export const ACCOUNT_ID = /^(?!__proto__$|constructor$|prototype$)[a-z0-9][a-z0-9_-]{0,63}$/;
const METHODS = new Set(['config.get', 'config.set', 'channels.status', 'channels.start',
  'channels.stop', 'channels.logout', 'web.login.start', 'web.login.wait']);
const QR = /^data:image\/png;base64,[A-Za-z0-9+/=]+$/;
const terminal = new Set(['connected', 'expired', 'failed']);
export class ControlError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export function createControlTransport(GatewayClient, { token, port = '18789' }) {
  let client;
  let ready = false;
  let closed = false;
  const waiters = new Set();
  function settle(error) {
    for (const waiter of waiters) {
      clearTimeout(waiter.timer);
      if (error) waiter.reject(error);
      else waiter.resolve();
    }
    waiters.clear();
  }
  function connect() {
    if (closed) return Promise.reject(new ControlError(503, 'Account controls are closed.'));
    if (ready && client?.connected !== false) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const waiter = { resolve, reject };
      waiter.timer = setTimeout(() => {
        waiters.delete(waiter);
        reject(new ControlError(503, 'Gateway connection timed out; it may be in overnight standby until 05:45 Africa/Algiers.'));
      }, 8000);
      waiters.add(waiter);
      if (client) return; // The same SDK client owns reconnects, never a second one.
      try {
        client = new GatewayClient({
          url: `ws://127.0.0.1:${port}`, token,
          clientName: 'gateway-client', clientDisplayName: 'Genaie account controls',
          mode: 'backend', role: 'operator', scopes: ['operator.admin'],
          deviceIdentity: null, sharedStateMode: 'read-only',
          onHelloOk: () => { if (!closed) { ready = true; settle(); } },
          onConnectError: () => { ready = false; settle(new ControlError(503, 'Gateway is unavailable; it may be in overnight standby until 05:45 Africa/Algiers.')); },
          onClose: () => { ready = false; settle(new ControlError(503, 'Gateway connection closed. Refresh status before retrying.')); },
        });
        client.start();
      } catch {
        client?.stop?.();
        client = undefined;
        settle(new ControlError(503, 'Gateway client could not start.'));
      }
    });
  }
  return {
    async request(method, params = {}) {
      if (!METHODS.has(method)) throw new ControlError(400, 'Unsupported gateway operation.');
      await connect();
      try { return await client.request(method, params, { timeoutMs: 20_000 }); }
      catch (error) {
        // Do not forward gateway details: config errors can contain secrets.
        if (method === 'config.set' && /hash|config changed|conflict/i.test(error.message ?? '')) {
          throw new ControlError(409, 'Configuration changed. Refresh status before retrying.');
        }
        throw new ControlError(502, 'Gateway operation could not be confirmed. Refresh status before retrying.');
      }
    },
    async close() {
      closed = true; ready = false;
      settle(new ControlError(503, 'Account controls closed.'));
      await client?.stopAndWait({ timeoutMs: 2000 });
    },
  };
}

// channels.status can omit auth identity for disabled accounts. Read only the
// persisted login's presence and phone identity; never return credential content.
export async function readAccountIdentity(config, id) {
  const state = process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw');
  const oauth = process.env.OPENCLAW_OAUTH_DIR?.trim() || path.join(state, 'credentials');
  const credentials = oauth.startsWith('~/') ? path.join(os.homedir(), oauth.slice(2)) : oauth;
  let directory = config.channels?.whatsapp?.accounts?.[id]?.authDir ?? config.channels?.whatsapp?.authDir;
  if (directory?.startsWith('~/')) directory = path.join(os.homedir(), directory.slice(2));
  directory ||= path.join(credentials, 'whatsapp', id);
  const candidates = [path.join(directory, 'creds.json')];
  if (id === 'default' && !config.channels?.whatsapp?.accounts?.[id]?.authDir && !config.channels?.whatsapp?.authDir) {
    candidates.push(path.join(credentials, 'creds.json'));
  }
  for (const filename of candidates) {
    try {
      const value = JSON.parse(await readFile(filename, 'utf8'));
      const match = /^(\d+)(?::\d+)?@s\.whatsapp\.net$/.exec(value?.me?.id ?? '');
      return { linked: true, number: match ? `+${match[1]}` : null };
    } catch (error) {
      if (error.code !== 'ENOENT') return { linked: null, number: null };
    }
  }
  return { linked: false, number: null };
}

export function createAccountController(gateway, { now = Date.now, killSwitch = false, readIdentity = readAccountIdentity } = {}) {
  const sessions = new Map();
  const params = (accountId) => ({ channel: 'whatsapp', accountId });
  async function snapshot() {
    const value = await gateway.request('config.get');
    if (!value.valid || !value.hash || !value.config) throw new ControlError(503, 'Gateway configuration is unavailable or invalid.');
    return value;
  }
  function accountIds(config) {
    const channel = config.channels?.whatsapp;
    const ids = Object.keys(channel?.accounts ?? {});
    if (ids.length === 0 || typeof channel?.authDir === 'string' && channel.authDir.trim()) ids.push('default');
    return [...new Set(ids)];
  }
  function requireAccount(config, accountId) {
    if (typeof accountId !== 'string' || !ACCOUNT_ID.test(accountId)) throw new ControlError(400, 'Use an account ID of 1–64 lowercase letters, digits, underscores or hyphens.');
    if (!accountIds(config).includes(accountId)) throw new ControlError(404, 'Account does not exist.');
  }
  function checkStart(config, accountId) {
    if (accountId === 'default' && killSwitch) throw new ControlError(409, 'REPLIES_DISABLED prevents starting the default account.');
    if (config.channels?.whatsapp?.enabled === false) throw new ControlError(409, 'WhatsApp is disabled in gateway configuration.');
  }
  async function setEnabled(accountId, enabled, { create = false } = {}) {
    const snap = await snapshot();
    const ids = accountIds(snap.config);
    if (create && ids.includes(accountId)) throw new ControlError(409, 'This account already exists. Link it from its account card.');
    if (!create) requireAccount(snap.config, accountId);
    if (enabled) checkStart(snap.config, accountId);
    // Use the authored object to preserve environment references and unknown fields.
    // config.set restores redacted secrets and rejects a stale baseHash atomically.
    const config = structuredClone(snap.parsed ?? snap.sourceConfig ?? snap.config);
    config.channels ??= {};
    config.channels.whatsapp ??= {};
    config.channels.whatsapp.accounts ??= {};
    const accounts = config.channels.whatsapp.accounts;
    if (!create && accounts[accountId]?.enabled === enabled && isApplied(snap)) return;
    accounts[accountId] = { ...accounts[accountId], enabled,
      ...(create ? { dmPolicy: 'disabled', groupPolicy: 'disabled' } : {}) };
    await gateway.request('config.set', { baseHash: snap.hash, raw: JSON.stringify(config) });
    // Publication can be deferred while work drains. Never act on old runtime config.
    for (let attempt = 0; attempt < 30; attempt++) {
      const applied = await snapshot();
      if (applied.config.channels?.whatsapp?.accounts?.[accountId]?.enabled === enabled &&
          isApplied(applied)) return;
      await delay(200);
    }
    throw new ControlError(409, 'Account setting is saved; gateway configuration is still being applied. Refresh status before retrying.');
  }
  function isApplied(snap) {
    return typeof snap.configRevisionHash === 'string' && snap.configRevisionHash.length > 0 &&
      snap.configRevisionHash === snap.appliedConfigHash;
  }
  async function status() {
    const snap = await snapshot();
    if (!isApplied(snap)) throw new ControlError(503, 'Gateway configuration is still being applied. Refresh account status shortly.');
    const live = await gateway.request('channels.status', { channel: 'whatsapp', probe: false });
    const after = await snapshot();
    if (!isApplied(after) || snap.hash !== after.hash) {
      throw new ControlError(503, 'Gateway configuration changed during the status read. Refresh account status shortly.');
    }
    if (live.partial || !Array.isArray(live.channelAccounts?.whatsapp)) {
      throw new ControlError(503, 'Gateway account status is incomplete. Refresh after its configuration reload finishes.');
    }
    const rows = live.channelAccounts.whatsapp;
    return { observedAt: new Date(now()).toISOString(), accounts: await Promise.all(accountIds(snap.config).map(async (id) => {
      const row = rows.find((entry) => entry.accountId === id);
      const configured = snap.config.channels?.whatsapp?.accounts?.[id];
      const enabled = configured?.enabled !== false && snap.config.channels?.whatsapp?.enabled !== false;
      const identity = await readIdentity(snap.config, id);
      const number = row?.self?.e164 ?? identity.number;
      return { id, number, enabled, linked: typeof row?.linked === 'boolean' ? row.linked : identity.linked,
        running: typeof row?.running === 'boolean' ? row.running : null,
        connected: typeof row?.connected === 'boolean' ? row.connected : null,
        repliesEnabled: (configured?.dmPolicy ?? snap.config.channels?.whatsapp?.dmPolicy) !== 'disabled',
        startBlockedReason: id === 'default' && killSwitch ? 'REPLIES_DISABLED prevents starting this account.' :
          snap.config.channels?.whatsapp?.enabled === false ? 'WhatsApp is disabled in gateway configuration.' : null,
        hasError: Boolean(row?.lastError), pairingSessionId: activeSession(id)?.id ?? null,
      };
    })) };
  }
  function expireSessions() {
    for (const [id, session] of sessions) {
      if (now() >= session.expiresAt && !terminal.has(session.state)) {
        session.state = 'expired'; session.qrDataUrl = null;
      }
      if (now() >= session.expiresAt + 60_000) sessions.delete(id);
    }
  }
  function activeSession(accountId) {
    expireSessions();
    return [...sessions.values()].find((s) => s.accountId === accountId && !terminal.has(s.state));
  }
  function publicSession(session) {
    return { id: session.id, accountId: session.accountId, state: session.state,
      qrDataUrl: session.qrDataUrl, expiresAt: new Date(session.expiresAt).toISOString() };
  }
  function updateQr(session, result) {
    if (result.sessionKey) session.gatewaySessionKey = result.sessionKey;
    if (result.qrDataUrl && QR.test(result.qrDataUrl) && result.qrDataUrl.length <= 16384) {
      session.qrDataUrl = result.qrDataUrl;
    }
  }
  async function finishLink(session) {
    await setEnabled(session.accountId, true);
    await gateway.request('channels.start', params(session.accountId));
    const current = await status();
    const account = current.accounts.find((a) => a.id === session.accountId);
    if (!account?.enabled || !account.linked || !account.running) throw new ControlError(502, 'Linked, but activation is not confirmed yet. Refresh account status.');
    session.state = 'connected'; session.qrDataUrl = null;
  }
  return {
    async dispatch(input) {
      const { action, accountId } = input;
      if (action === 'list') return status();
      if (action === 'link-status') {
        expireSessions();
        const session = sessions.get(input.sessionId);
        if (!session) throw new ControlError(404, 'Pairing session expired. Start Link again.');
        if (!terminal.has(session.state)) {
          try {
            const result = await gateway.request('web.login.wait', { ...params(session.accountId), timeoutMs: 1000,
              ...(session.gatewaySessionKey ? { sessionKey: session.gatewaySessionKey } : {}),
              ...(session.qrDataUrl ? { currentQrDataUrl: session.qrDataUrl } : {}) });
            updateQr(session, result);
            if (result.connected) await finishLink(session);
            else if (result.expired || /expired|no active .*login/i.test(result.message ?? '')) { session.state = 'expired'; session.qrDataUrl = null; }
            else if (/failed|ended without|logged out/i.test(result.message ?? '')) { session.state = 'failed'; session.qrDataUrl = null; }
          } catch (error) { session.state = 'failed'; session.qrDataUrl = null; throw error; }
        }
        return publicSession(session);
      }
      if (typeof accountId !== 'string' || !ACCOUNT_ID.test(accountId)) throw new ControlError(400, 'Use an account ID of 1–64 lowercase letters, digits, underscores or hyphens.');
      if (!['link', 'unlink', 'pause', 'start'].includes(action)) throw new ControlError(400, 'Unsupported account action.');
      const pending = activeSession(accountId);
      if (pending) {
        if (action === 'link') return publicSession(pending);
        throw new ControlError(409, 'A pairing attempt is already active for this account. Wait for it to finish or expire.');
      }
      const snap = await snapshot();
      if (action === 'link') {
        checkStart(snap.config, accountId);
        if (sessions.size >= 32) throw new ControlError(409, 'Too many pairing sessions. Wait for older sessions to expire.');
        if (input.create) await setEnabled(accountId, false, { create: true });
        else requireAccount(snap.config, accountId);
        const session = { id: randomUUID(), accountId, state: 'waiting', qrDataUrl: null, expiresAt: now() + 180_000 };
        sessions.set(session.id, session);
        try {
          const result = await gateway.request('web.login.start', { ...params(accountId), force: false, timeoutMs: 10_000 });
          updateQr(session, result);
          if (result.connected || !result.qrDataUrl && (await status()).accounts.find((a) => a.id === accountId)?.linked) await finishLink(session);
          else if (!session.qrDataUrl) throw new ControlError(502, 'Could not generate a WhatsApp QR. Refresh status and try Link again.');
          return publicSession(session);
        } catch (error) { session.state = 'failed'; throw error; }
      }
      requireAccount(snap.config, accountId);
      if (action === 'start') checkStart(snap.config, accountId);
      // The gateway owns QR sockets, so they can survive this API process.
      // Probe its existing login before changing state; never initiate a new one.
      const login = await gateway.request('web.login.wait', { ...params(accountId), timeoutMs: 1000 });
      if (!login.connected && !/no active .*login|expired|failed|ended without|logged out/i.test(login.message ?? '')) {
        throw new ControlError(409, 'WhatsApp pairing is still active. Continue linking or wait up to three minutes for it to expire.');
      }
      if (action === 'start') {
        const current = (await status()).accounts.find((a) => a.id === accountId);
        if (current?.linked !== true) throw new ControlError(409, 'This account needs a WhatsApp login. Use Link.');
        await setEnabled(accountId, true);
        const result = await gateway.request('channels.start', params(accountId));
        if (!result.started) throw new ControlError(502, 'Start is not confirmed yet. Refresh status before retrying.');
      } else {
        // Persist first so watchdogs and future boots cannot undo the stop.
        await setEnabled(accountId, false);
        const result = await gateway.request('channels.stop', params(accountId));
        if (!result.stopped) throw new ControlError(502, 'Stop is not confirmed yet. Refresh status before retrying.');
        if (action === 'unlink') {
          const logout = await gateway.request('channels.logout', params(accountId));
          if (!logout.cleared && !logout.loggedOut) throw new ControlError(502, 'WhatsApp logout could not be confirmed. Refresh status before retrying.');
        }
      }
      const current = await status();
      const account = current.accounts.find((a) => a.id === accountId);
      if ((action === 'start' && (!account?.enabled || account.running !== true || account.linked !== true)) ||
          (action !== 'start' && (account?.enabled !== false || account?.running !== false)) ||
          (action === 'unlink' && account?.linked !== false)) {
        throw new ControlError(502, 'Account operation is not confirmed yet. Refresh status before retrying.');
      }
      return current;
    },
  };
}

async function main() {
  if (!process.env.GATEWAY_AUTH_TOKEN) throw new Error('GATEWAY_AUTH_TOKEN is required');
  const Client = await loadCampaignGatewayClient();
  const gateway = createControlTransport(Client, { token: process.env.GATEWAY_AUTH_TOKEN,
    port: process.env.OPENCLAW_GATEWAY_PORT || '18789' });
  const killSwitch = /^(1|true|yes|on)$/i.test((process.env.REPLIES_DISABLED ?? '').trim().replace(/^["']|["']$/g, ''));
  const controller = createAccountController(gateway, { killSwitch });
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  try {
    for await (const line of lines) {
      try {
        const payload = await controller.dispatch(JSON.parse(line));
        process.stdout.write(JSON.stringify({ ok: true, payload }) + '\n');
      } catch (error) {
        process.stdout.write(JSON.stringify({ ok: false, status: error instanceof ControlError ? error.status : 502,
          error: error instanceof ControlError ? error.message : 'Account control failed. Refresh status before retrying.' }) + '\n');
      }
    }
  } finally { await gateway.close(); }
}
if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  main().catch(() => { process.stderr.write('Account control bridge unavailable.\n'); process.exitCode = 1; });
}
