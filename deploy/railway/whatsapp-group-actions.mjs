// Operator-requested group actions, run on the account's own live socket.
//
// Leaving a WhatsApp group is the one thing the console cannot do for itself:
// the gateway exposes no RPC for it, and a second process cannot borrow an
// account's socket. So the console API drops a request file on the shared state
// volume and this module -- injected into the channel monitor beside the group
// inventory, holding the same socket -- performs it and writes back a result.
//
// The contract is deliberately small: one group leave per request, a result for
// every request, and never a rejection into the channel's lifecycle.
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { inventoryPath, pruneGroupFromInventory } from './whatsapp-group-discovery.mjs';

export const POLL_INTERVAL_MS = 1_000;
export const LEAVE_TIMEOUT_MS = 20_000;
// A result nobody collected is litter, not state. The bridge waits far less
// than this, so anything older has been abandoned.
export const RESULT_TTL_MS = 10 * 60 * 1000;
const ACCOUNT = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const GROUP = /^\d+(?:-\d+)?@g\.us$/;
const ID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
// A reconnect may replace the monitor before an old request finishes.
const owners = new Map();

/** Where an account's request queue lives. The console API writes here. */
export function actionsDirectory(root, accountId) {
  return path.join(root, 'runtime', 'whatsapp-group-actions', accountId);
}

async function atomicWrite(filename, value) {
  const temporary = `${filename}.${randomUUID()}.tmp`;
  let handle;
  try {
    handle = await fs.open(temporary, 'wx', 0o600);
    await handle.writeFile(JSON.stringify(value) + '\n');
    await handle.sync();
    await handle.close();
    handle = null;
    await fs.rename(temporary, filename);
  } finally {
    await handle?.close();
    await fs.rm(temporary, { force: true });
  }
}

/**
 * What a failed leave is called, in words safe to hand back to an operator.
 *
 * Baileys raises a Boom-shaped error. 403 and 404 mean the account is not in
 * the group, which is the end state the request asked for, so they are not
 * failures -- the inventory is still pruned, because it is what claims the
 * membership that does not exist.
 */
export function classifyLeaveError(error) {
  if (error?.leaveTimeout) return { ok: false, error: 'leave_timeout' };
  const status = error?.output?.statusCode ?? error?.data?.statusCode ?? error?.status;
  if (status === 403 || status === 404) return { ok: true, already: true };
  if (status === 401) return { ok: false, error: 'not_authorized' };
  return { ok: false, error: 'leave_failed' };
}

export function createWhatsAppGroupActions({ accountId,
  root = process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw'),
  environment = process.env, now = () => new Date(), warn = console.warn,
  intervalMs = POLL_INTERVAL_MS, timeoutMs = LEAVE_TIMEOUT_MS, resultTtlMs = RESULT_TTL_MS,
  setIntervalFn = setInterval, clearIntervalFn = clearInterval,
} = {}) {
  const enabled = environment.GENAIE_GROUP_ACTIONS !== '0' && ACCOUNT.test(accountId ?? '') &&
    !['__proto__', 'constructor', 'prototype'].includes(accountId);
  const directory = actionsDirectory(root, accountId);
  const requests = path.join(directory, 'requests');
  const results = path.join(directory, 'results');
  const owner = {};
  let socket, timer, generation = 0, inFlight;

  function stop() {
    generation++;
    socket = null;
    inFlight = null;
    if (timer !== undefined) clearIntervalFn(timer);
    timer = undefined;
    if (owners.get(directory) === owner) owners.delete(directory);
  }

  async function leave(sock, jid) {
    let deadline;
    try {
      await Promise.race([
        Promise.resolve().then(() => sock.groupLeave(jid)),
        // Deliberately NOT unref'd, unlike the poll interval: this timer is
        // what guarantees every queued request gets an answer, so it has to
        // keep the loop alive until it fires.
        new Promise((_, reject) => {
          deadline = setTimeout(() => reject(Object.assign(new Error(), { leaveTimeout: true })), timeoutMs);
        }),
      ]);
      return { ok: true, already: false };
    } catch (error) {
      return classifyLeaveError(error);
    } finally {
      clearTimeout(deadline);
    }
  }

  /** One request: leave, prune, answer. A request that cannot be understood
   *  still gets an answer, so the bridge never waits out its whole budget on a
   *  file it was never going to be told about. */
  async function handle(sock, filename, current) {
    let request = null;
    try {
      request = JSON.parse(await fs.readFile(filename, 'utf8'));
    } catch { /* unreadable or half-written; answered as invalid below */ }

    const id = typeof request?.id === 'string' && ID.test(request.id) ? request.id : null;
    const jid = typeof request?.jid === 'string' && request.jid.length <= 128 &&
      GROUP.test(request.jid) ? request.jid : null;

    // Without a usable id there is nowhere to put an answer; drop the file so
    // it cannot be retried forever.
    if (!id) {
      await fs.rm(filename, { force: true });
      return;
    }

    const outcome = jid ? await leave(sock, jid) : { ok: false, error: 'invalid_request' };
    if (outcome.ok && jid && current()) {
      try {
        await pruneGroupFromInventory(inventoryPath(root, accountId), accountId, jid);
      } catch {
        // The account has left; an inventory that still lists the group is a
        // stale read, not a failed leave. The next scan corrects it.
        warn(`[whatsapp-group-actions] ${accountId}: inventory prune unavailable`);
      }
    }
    await atomicWrite(path.join(results, `${id}.json`), {
      id, jid, ok: Boolean(outcome.ok), already: Boolean(outcome.already),
      error: outcome.ok ? null : outcome.error, finishedAt: now().toISOString(),
    });
    await fs.rm(filename, { force: true });
  }

  async function sweep() {
    const cutoff = now().getTime() - resultTtlMs;
    let entries;
    try { entries = await fs.readdir(results); } catch { return; }
    for (const entry of entries) {
      if (!entry.endsWith('.json')) continue;
      const filename = path.join(results, entry);
      try {
        const stats = await fs.stat(filename);
        if (stats.mtimeMs < cutoff) await fs.rm(filename, { force: true });
      } catch { /* raced with a collector; nothing to clean */ }
    }
  }

  function poll() {
    if (!enabled || !socket || owners.get(directory) !== owner) return Promise.resolve();
    if (inFlight) return inFlight;
    const epoch = generation, sock = socket;
    const current = () => generation === epoch && owners.get(directory) === owner && socket === sock;
    inFlight = (async () => {
      let entries;
      try { entries = await fs.readdir(requests); } catch { return; }
      for (const entry of entries) {
        if (!current()) return;
        // Only this module's own filenames, so a stray path can never be
        // followed out of the queue directory.
        if (!entry.endsWith('.json') || !ID.test(entry.slice(0, -5))) continue;
        await handle(sock, path.join(requests, entry), current);
      }
      await sweep();
    })().catch(() => {
      // Group actions must never reject into the channel's lifecycle, and must
      // never log raw provider errors.
      if (current()) warn(`[whatsapp-group-actions] ${accountId}: request handling unavailable`);
    }).finally(() => { inFlight = null; });
    return inFlight;
  }

  function start(sock) {
    stop();
    if (!enabled) return;
    if (typeof sock?.groupLeave !== 'function') {
      // The load-time patch guards the lifecycle anchors, not the socket shape.
      warn(`[whatsapp-group-actions] ${accountId}: account socket cannot leave groups; actions inactive`);
      return;
    }
    socket = sock;
    owners.set(directory, owner);
    void fs.mkdir(requests, { recursive: true, mode: 0o700 })
      .then(() => fs.mkdir(results, { recursive: true, mode: 0o700 }))
      .then(() => { void poll(); })
      .catch(() => { warn(`[whatsapp-group-actions] ${accountId}: queue unavailable`); });
    timer = setIntervalFn(() => { void poll(); }, intervalMs);
    timer?.unref?.();
  }
  return { start, stop, poll };
}
