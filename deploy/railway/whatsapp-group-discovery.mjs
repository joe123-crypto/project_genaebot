// Metadata inventory only. Never opens a connection or changes ad targeting.
// `pruneGroupFromInventory` is the one operator-driven edit: it removes a single
// group after the account has actually left it, so the inventory stops claiming a
// membership that no longer exists. It goes through this module, rather than the
// caller writing the file, because `owners` and `writes` below are module-scoped:
// a write from anywhere else would not be ordered against a scan in flight.
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { observeWhatsAppGroups } from './whatsapp-group-activity.mjs';

export const DISCOVERY_INTERVAL_MS = 60 * 60 * 1000;
export const DISCOVERY_TIMEOUT_MS = 45_000;
const ACCOUNT = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const GROUP = /^\d+(?:-\d+)?@g\.us$/;
// A reconnect may replace the monitor before its old request finishes.
const owners = new Map();
const writes = new Map();

/** Where an account's inventory lives. Shared so a caller that prunes an entry
 *  cannot name a different file than the scan that writes it. */
export function inventoryPath(root, accountId) {
  return path.join(root, 'runtime', 'whatsapp-groups', `${accountId}.json`);
}

async function serialWrite(key, task) {
  const pending = (writes.get(key) ?? Promise.resolve()).catch(() => {}).then(task);
  writes.set(key, pending);
  try { return await pending; }
  finally { if (writes.get(key) === pending) writes.delete(key); }
}

async function readInventory(filename, accountId) {
  try {
    const value = JSON.parse(await fs.readFile(filename, 'utf8'));
    if (value?.version !== 1 || value.accountId !== accountId || !value.groups ||
        typeof value.groups !== 'object' || Array.isArray(value.groups)) throw new Error('Invalid inventory');
    for (const [jid, group] of Object.entries(value.groups)) {
      if (!GROUP.test(jid) || !group ||
          !(group.name === null || typeof group.name === 'string') ||
          typeof group.firstSeenAt !== 'string' || typeof group.lastSeenAt !== 'string' ||
          !Number.isFinite(Date.parse(group.firstSeenAt)) ||
          !Number.isFinite(Date.parse(group.lastSeenAt))) throw new Error('Invalid inventory group');
    }
    return value;
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    return { version: 1, accountId, lastAttemptAt: null, lastSuccessAt: null, lastError: null, groups: {} };
  }
}

async function atomicSave(filename, value, current) {
  const directory = path.dirname(filename);
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const temporary = `${filename}.${randomUUID()}.tmp`;
  let handle;
  try {
    handle = await fs.open(temporary, 'wx', 0o600);
    await handle.writeFile(JSON.stringify(value) + '\n');
    await handle.sync();
    await handle.close();
    handle = null;
    if (current()) await fs.rename(temporary, filename);
  } finally {
    await handle?.close();
    await fs.rm(temporary, { force: true });
  }
}

/**
 * Drop one group from an account's inventory. Returns true when an entry was
 * actually removed, false when the inventory had none (already pruned, never
 * scanned, or unreadable -- none of which is an error worth failing a leave
 * over).
 *
 * A scan whose lookup finished before the leave can still re-add the group on
 * its way out; the next scan, at most an hour later, sees the truth and drops
 * it again.
 */
export async function pruneGroupFromInventory(filename, accountId, jid) {
  if (!GROUP.test(jid ?? '')) throw new Error('Invalid group id');
  return serialWrite(filename, async () => {
    let inventory;
    try {
      inventory = await readInventory(filename, accountId);
    } catch {
      return false;
    }
    if (!Object.prototype.hasOwnProperty.call(inventory.groups, jid)) return false;
    delete inventory.groups[jid];
    await atomicSave(filename, inventory, () => true);
    return true;
  });
}

export function createWhatsAppGroupDiscovery({ accountId,
  root = process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw'),
  environment = process.env, now = () => new Date(), warn = console.warn,
  intervalMs = DISCOVERY_INTERVAL_MS, timeoutMs = DISCOVERY_TIMEOUT_MS,
  setIntervalFn = setInterval, clearIntervalFn = clearInterval,
} = {}) {
  const enabled = environment.GENAIE_GROUP_DISCOVERY !== '0' && ACCOUNT.test(accountId ?? '') &&
    !['__proto__', 'constructor', 'prototype'].includes(accountId);
  const filename = inventoryPath(root, accountId);
  const owner = {};
  let socket, timer, generation = 0, inFlight;

  function stop() {
    generation++;
    socket = null;
    inFlight = null;
    if (timer !== undefined) clearIntervalFn(timer);
    timer = undefined;
    if (owners.get(filename) === owner) owners.delete(filename);
  }

  function scan() {
    if (!enabled || !socket || owners.get(filename) !== owner) return Promise.resolve();
    if (inFlight) return inFlight.done;
    const epoch = generation, sock = socket;
    const current = () => generation === epoch && owners.get(filename) === owner && socket === sock;
    const attempt = { settled: false, finished: false, done: null };
    inFlight = attempt;
    const release = () => {
      if (attempt.settled && attempt.finished && inFlight === attempt) inFlight = null;
    };
    const fetch = Promise.resolve().then(() => sock.groupFetchAllParticipating());
    // A timed-out query may still be pending on Baileys. Do not pile another
    // request onto that socket until it settles (or the connection is replaced).
    fetch.then(() => { attempt.settled = true; release(); }, () => { attempt.settled = true; release(); });
    attempt.done = (async () => {
      let deadline;
      let groups, failure = null;
      try {
        groups = await Promise.race([fetch, new Promise((_, reject) => {
          deadline = setTimeout(() => reject(Object.assign(new Error(), { discoveryTimeout: true })), timeoutMs);
          deadline.unref?.();
        })]);
        if (!groups || typeof groups !== 'object' || Array.isArray(groups)) failure = 'invalid_metadata';
      } catch (error) {
        failure = error?.discoveryTimeout ? 'lookup_timeout' : 'lookup_failed';
      } finally { clearTimeout(deadline); }
      if (!current()) return;
      const stamp = now().toISOString();
      try {
        await serialWrite(filename, async () => {
          if (!current()) return;
          const inventory = await readInventory(filename, accountId);
          if (!current()) return;
          inventory.lastAttemptAt = stamp;
          inventory.lastError = failure;
          if (!failure) {
            observeWhatsAppGroups(accountId, groups, root);
            inventory.lastSuccessAt = stamp;
            for (const [jid, metadata] of Object.entries(groups)) {
              if (!GROUP.test(jid) || jid.length > 128) continue;
              const previous = inventory.groups[jid];
              const name = typeof metadata?.subject === 'string'
                ? Array.from(metadata.subject.trim().toWellFormed()).slice(0, 512).join('') : '';
              const memberCount = Number.isSafeInteger(metadata?.size) && metadata.size >= 0 ? metadata.size
                : Array.isArray(metadata?.participants) ? metadata.participants.length : null;
              inventory.groups[jid] = { name: name || previous?.name || null,
                firstSeenAt: previous?.firstSeenAt ?? stamp, lastSeenAt: stamp,
                memberCount: memberCount ?? previous?.memberCount ?? null,
                memberCountObservedAt: memberCount !== null ? stamp : previous?.memberCountObservedAt ?? null };
            }
          }
          await atomicSave(filename, inventory, current);
        });
        if (failure && current()) warn(`[whatsapp-groups] ${accountId}: ${failure}; previous observations retained`);
      } catch {
        if (current()) warn(`[whatsapp-groups] ${accountId}: inventory unavailable; targeting unchanged`);
      }
    })().catch(() => {
      // Discovery must never reject into the channel's lifecycle or log raw
      // provider errors (which may include private transport details).
      if (current()) warn(`[whatsapp-groups] ${accountId}: discovery unavailable`);
    }).finally(() => { attempt.finished = true; release(); });
    return attempt.done;
  }

  function start(sock) {
    stop();
    if (!enabled) return;
    if (typeof sock?.groupFetchAllParticipating !== 'function') {
      // The load-time patch guards the lifecycle anchors, not the socket shape.
      // Say so rather than going quiet, so an adapter change is diagnosable.
      warn(`[whatsapp-groups] ${accountId}: account socket has no group lookup; discovery inactive`);
      return;
    }
    socket = sock;
    owners.set(filename, owner);
    void scan();
    timer = setIntervalFn(() => { void scan(); }, intervalMs);
    timer?.unref?.();
  }
  return { start, stop, scan };
}
