// Records that a person answered a customer by hand, so the bot can stop
// replying in that chat.
//
// The operator runs the business from the same WhatsApp account as the bot, so
// a reply typed on that phone is an OUTBOUND message: no inbound event is
// ever raised for it and no gateway plugin can see it. The adapter does see it,
// one step before throwing it away. By the time inbound access control reaches
// its "Skipping outbound DM (fromMe)" branch the message has already survived
//
//   - the recent-outbound dedupe cache, which drops the echo of anything the
//     bot itself sent (every bot send goes through sendTrackedMessage), and
//   - hasInboundUserContent, which drops receipts, typing notices, reactions
//     and the protocol message that a deletion really is,
//
// so what is left is a non-group message, with real content, sent from this
// account by something other than the bot. That is the operator typing. This
// module is injected at that branch (see whatsapp-lid-loader.mjs) and writes
// the peer into a store the public-service-gate plugin reads.
//
// Writing is the only thing it does. It never replies, never touches the
// socket, and never throws into the channel's inbound path: a storage failure
// warns once and leaves the bot talking, which is the behaviour we have today.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const TAKEOVER_FILE = 'takeovers.json';
// Far more chats than this line will ever hold. The cap is only here so a
// store that somehow keeps growing cannot grow without bound.
export const MAX_ENTRIES = 5000;
// After a reconnect WhatsApp replays recent traffic, the bot's own sends
// included. The adapter drops those replays as stale appends, but only AFTER
// inbound access control has run, so this module would see them. It uses the
// adapter's own rule (APPEND_RECENT_GRACE_MS in shouldSkipStaleAppend) to
// ignore anything that predates the current connection: the dedupe cache that
// tells a bot send from a person's only holds 20 minutes, so an older replay
// cannot be classified safely. The cost is a reply typed while the gateway was
// disconnected, which is missed. That errs toward the bot talking, which is
// what it does today.
export const CONNECT_GRACE_MS = 60_000;

const NON_CHAT = /@(?:g\.us|status|broadcast|newsletter)$/;
const rootPath = () => process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw');
let warned = false;

/**
 * Where the store lives. Must stay in step with getTakeoverStorePath() in
 * workspace/plugins/public-service-gate/lib/takeover.js, which derives the same
 * path from the plugin's serviceStatePath; a contract test asserts they agree.
 */
export function takeoverStorePath(root = rootPath()) {
  return path.join(root, 'runtime', 'public-service-gate', TAKEOVER_FILE);
}

/**
 * The plugin keys every per-peer store by normalizePhone() from
 * workspace/plugins/public-service-gate/lib/events.js. This module cannot
 * import it (it is baked into the image beside the adapter, not deployed with
 * the workspace), so it reproduces it: a phone JID becomes +E164, an opaque
 * WhatsApp id keeps its @lid / @hosted.lid identity, and anything else with an
 * @ is not an identity we can key on.
 */
export function takeoverKey(value) {
  if (typeof value !== 'string') return '';
  const trimmed = value.trim();
  if (!trimmed) return '';
  const lid = /^(\d{1,32})(?::\d+)?@(lid|hosted\.lid)$/.exec(trimmed);
  if (lid) return `${lid[1]}@${lid[2]}`;
  const phoneJid = /^(\d{8,15})(?::\d+)?@(s\.whatsapp\.net|hosted)$/.exec(trimmed);
  if (phoneJid) return `+${phoneJid[1]}`;
  if (trimmed.includes('@')) return '';
  const plus = trimmed.startsWith('+') ? '+' : '';
  return plus + trimmed.replace(/[^\d]/g, '');
}

/**
 * Both spellings of the peer, deduplicated. `from` is the identity the adapter
 * resolved and therefore the one the gate will look up; `remoteJid` is the raw
 * address, recorded too in case the two ever disagree.
 */
export function takeoverKeys({ from, remoteJid } = {}) {
  const keys = [];
  for (const candidate of [from, remoteJid]) {
    const key = takeoverKey(candidate);
    if (key && !keys.includes(key)) keys.push(key);
  }
  return keys;
}

function readTakeovers(filePath) {
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    const takeovers = parsed?.takeovers;
    return takeovers && typeof takeovers === 'object' && !Array.isArray(takeovers) ? takeovers : {};
  } catch {
    return {};
  }
}

function writeTakeovers(filePath, takeovers) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmpPath = path.join(path.dirname(filePath), `.${TAKEOVER_FILE}.${process.pid}.tmp`);
  fs.writeFileSync(tmpPath, JSON.stringify({ version: 1, takeovers }, null, 2) + '\n', {
    encoding: 'utf8',
    mode: 0o600,
  });
  fs.renameSync(tmpPath, filePath);
}

function prune(takeovers) {
  const entries = Object.entries(takeovers);
  if (entries.length <= MAX_ENTRIES) return takeovers;
  entries.sort((a, b) => String(b[1]?.at ?? '').localeCompare(String(a[1]?.at ?? '')));
  return Object.fromEntries(entries.slice(0, MAX_ENTRIES));
}

/**
 * True when the message predates the current connection, which makes it part of
 * a reconnect replay rather than something a person just typed.
 */
function isReplayedMessage(messageTimestampMs, connectedAtMs) {
  if (!Number.isFinite(messageTimestampMs) || !Number.isFinite(connectedAtMs)) return false;
  return messageTimestampMs < connectedAtMs - CONNECT_GRACE_MS;
}

function report(warn, error) {
  if (warned) return;
  warned = true;
  warn(`[whatsapp-takeover] could not record an operator reply; the bot may keep replying to that chat (${String(error)})`);
}

/** Test seam: the warning is once per process, which would leak across tests. */
export function resetHumanTakeoverWarning() {
  warned = false;
}

/**
 * Record a reply the operator typed by hand. Returns the keys written, or null
 * when there was nothing to record.
 */
export function noteHumanTakeover({
  accountId,
  from,
  remoteJid,
  group,
  selfE164,
  messageTimestampMs,
  connectedAtMs,
  root = rootPath(),
  environment = process.env,
  now = Date.now,
  warn = console.warn,
} = {}) {
  try {
    if (environment.GENAIE_HUMAN_TAKEOVER === '0') return null;
    if (group === true) return null;
    if (isReplayedMessage(messageTimestampMs, connectedAtMs)) return null;
    // Groups are reply-disabled anyway, and a status or broadcast address is
    // not a chat a person can be handed.
    if (NON_CHAT.test(typeof remoteJid === 'string' ? remoteJid.trim() : '')) return null;
    // The operator's self-chat is the channel for talking to the bot. Taking it
    // over would silence the operator's own admin commands.
    const own = takeoverKey(typeof selfE164 === 'string' ? selfE164 : '');
    const keys = takeoverKeys({ from, remoteJid }).filter((key) => key !== own);
    if (!keys.length) return null;

    const filePath = takeoverStorePath(root);
    const takeovers = readTakeovers(filePath);
    // A person answering properly sends several messages. Only the first is a
    // takeover; the rest must not rewrite the store or move the timestamp,
    // which is what an operator "resume" is compared against.
    const missing = keys.filter((key) => !takeovers[key]);
    if (!missing.length) return null;

    const at = new Date(now()).toISOString();
    // Every spelling of this chat is recorded on every entry, so the plugin's
    // "resume" can release all of them even when the operator names only one.
    for (const key of missing) takeovers[key] = { at, accountId: String(accountId || 'default'), peers: keys };
    writeTakeovers(filePath, prune(takeovers));
    return missing;
  } catch (error) {
    report(warn, error);
    return null;
  }
}
