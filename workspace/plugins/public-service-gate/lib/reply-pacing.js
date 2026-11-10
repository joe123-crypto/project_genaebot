import { parseBoolean, readEnvValue } from "./config.js";
import { normalizePhone } from "./events.js";

// ---------------------------------------------------------------------------
// Reply pacing
//
// WhatsApp cannot read a reply, but it can see how a line behaves. Left alone,
// this bot answers every customer at machine speed: the reply lands a few
// seconds after the message whatever its length, canned replies (the CV
// interview) land in milliseconds, and after the overnight standby a whole
// backlog of chats is answered in the same second. Nobody running a business
// from a phone looks like that.
//
// This module holds a customer reply for as long as a person would take:
//   - a pause before starting on the message (longer when the chat was idle,
//     because nobody is staring at a quiet chat);
//   - time to type the reply, in proportion to its length;
//   - a gap when the line moves from one chat to another, so two customers are
//     never answered at once.
//
// Only replies to a customer who has just written are paced. Proactive sends
// (promo broadcasts, admin alerts, partner forwards, CV deliveries) have no
// recent inbound on record and leave untouched, as do admin and group chats,
// which never get a record in the first place.
//
// Every wait is bounded and every failure sends the reply at once: a late reply
// is acceptable, a lost one is not. Like lib/sibling-lines.js this imports
// nothing from `openclaw`, so a module-resolution failure cannot stop the plugin
// loading.
// ---------------------------------------------------------------------------

export const REPLY_PACING_ENV = "GENAIE_REPLY_PACING";

// A chat with no exchange for this long is one nobody is looking at.
const IDLE_CHAT_MS = 5 * 60 * 1000;
// A reply is only paced while the inbound that caused it is this recent.
const INBOUND_TTL_MS = 10 * 60 * 1000;

const READ_IDLE_MS = [5000, 25000];
const READ_ACTIVE_MS = [2000, 8000];
const READ_PER_CHAR_MS = 30;
const READ_LENGTH_CAP_MS = 4000;

// Characters per second for someone typing on a phone.
const TYPING_CPS = [4, 8];
const TYPING_MS = [1500, 30000];

// Moving from one customer's chat to another's.
const CHAT_SWITCH_MS = [2000, 6000];

// No single reply is ever held longer than this, whatever the queue.
export const MAX_HOLD_MS = 90 * 1000;

const PRUNE_ABOVE = 500;

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Digits-only for phones, the full id for a WhatsApp LID, so the sender of an
// inbound and the recipient of its reply land on the same key (same reason
// lib/failsafe.js keeps its own key).
export function peerKey(value) {
  const identity = normalizePhone(String(value ?? ""));
  return identity.includes("@") ? identity : identity.replace(/[^\d]/g, "");
}

export function isReplyPacingEnabled() {
  return parseBoolean(readEnvValue(REPLY_PACING_ENV)) !== false;
}

export function createReplyPacer({ rng = Math.random, sleep = defaultSleep, now = Date.now } = {}) {
  const chats = new Map();
  const accounts = new Map();

  const between = ([min, max]) => min + (max - min) * rng();
  // Squaring skews toward the short end: most messages are picked up quickly,
  // a few are not.
  const skewed = ([min, max]) => min + (max - min) * rng() ** 2;
  const chatKey = (accountId, peer) => `${accountId || "default"}:${peer}`;

  function prune(at) {
    if (chats.size <= PRUNE_ABOVE) return;
    for (const [key, chat] of chats) {
      if (at - Math.max(chat.inboundAt, chat.lastSentAt) > INBOUND_TTL_MS) chats.delete(key);
    }
  }

  function freshChat(accountId, recipient, at) {
    const peer = peerKey(recipient);
    if (!peer) return null;
    const key = chatKey(accountId, peer);
    const chat = chats.get(key);
    if (!chat || at - chat.inboundAt > INBOUND_TTL_MS) return null;
    return { key, chat };
  }

  function typingMs(content) {
    const length = String(content ?? "").length;
    const [min, max] = TYPING_MS;
    return Math.min(max, Math.max(min, (length / between(TYPING_CPS)) * 1000));
  }

  /** Record a customer message. Call once per inbound, before any reply. */
  function noteInbound(accountId, sender, text = "") {
    const peer = peerKey(sender);
    if (!peer) return;
    const at = now();
    const key = chatKey(accountId, peer);
    const previous = chats.get(key);
    const idle = !previous || at - Math.max(previous.inboundAt, previous.lastSentAt) > IDLE_CHAT_MS;
    const lengthMs = Math.min(READ_LENGTH_CAP_MS, String(text ?? "").length * READ_PER_CHAR_MS);
    chats.set(key, {
      inboundAt: at,
      lastSentAt: previous?.lastSentAt ?? 0,
      readMs: (idle ? skewed(READ_IDLE_MS) : between(READ_ACTIVE_MS)) + lengthMs,
      // Where the typing clock starts: set once the read pause is over.
      startedAt: null,
    });
    prune(at);
  }

  /**
   * The pause before the bot starts on a message it is about to answer.
   * Resolves to the milliseconds waited, or null when the chat is not paced.
   */
  async function pauseBeforeReply(accountId, sender) {
    const found = freshChat(accountId, sender, now());
    if (!found) return null;
    if (found.chat.startedAt !== null) return 0;
    const { chat } = found;
    const wait = Math.max(0, Math.min(MAX_HOLD_MS, chat.readMs - (now() - chat.inboundAt)));
    if (wait > 0) await sleep(wait);
    chat.startedAt = now();
    return wait;
  }

  /**
   * Hold one outbound reply for as long as typing it would take. Resolves to
   * the milliseconds waited, or null when the recipient is not a paced chat.
   */
  async function pauseBeforeSend(accountId, recipient, content) {
    const at = now();
    const found = freshChat(accountId, recipient, at);
    if (!found) return null;
    const { key, chat } = found;

    // A reply that skipped the read pause (the gate answered without the
    // agent) still owes it.
    const owedFrom = chat.startedAt ?? chat.inboundAt + chat.readMs;
    let sendAt = Math.max(at, owedFrom + typingMs(content));

    const account = accounts.get(accountId || "default");
    if (account && account.chat !== key) sendAt = Math.max(sendAt, account.freeAt + between(CHAT_SWITCH_MS));
    sendAt = Math.min(sendAt, at + MAX_HOLD_MS);

    // Reserve the slot before sleeping, so replies queued behind this one line
    // up after it instead of all waking together.
    accounts.set(accountId || "default", { chat: key, freeAt: sendAt });
    chat.startedAt = sendAt;
    chat.lastSentAt = sendAt;

    const wait = sendAt - at;
    if (wait > 0) await sleep(wait);
    return wait;
  }

  return { noteInbound, pauseBeforeReply, pauseBeforeSend };
}

let sharedPacer = createReplyPacer();

export function getReplyPacer() {
  return sharedPacer;
}

/** Test seam: swap the shared pacer's clock, sleep and randomness. */
export function resetReplyPacer(runtime) {
  sharedPacer = createReplyPacer(runtime);
  return sharedPacer;
}
