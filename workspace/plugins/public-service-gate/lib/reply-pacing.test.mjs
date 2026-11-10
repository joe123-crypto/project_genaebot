import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_HOLD_MS,
  REPLY_PACING_ENV,
  createReplyPacer,
  isReplyPacingEnabled,
  peerKey,
} from "./reply-pacing.js";

const CUSTOMER = "+263771234567";
const OTHER = "+263772222222";

// A clock that only moves when the pacer sleeps (or the test advances it), and
// a fixed random draw so every range resolves to a known point.
function harness(draw = 0.5) {
  const clock = { at: 1_000_000 };
  const sleeps = [];
  const pacer = createReplyPacer({
    rng: () => draw,
    now: () => clock.at,
    sleep: async (ms) => {
      sleeps.push(ms);
      clock.at += ms;
    },
  });
  return { clock, sleeps, pacer };
}

test("a first message is not picked up at once, and a short reply still takes typing time", async () => {
  const { clock, pacer } = harness();
  const arrived = clock.at;
  pacer.noteInbound("default", CUSTOMER, "Hi");

  const read = await pacer.pauseBeforeReply("default", CUSTOMER);
  assert.ok(read >= 5000 && read <= 29000, `read pause ${read}`);

  clock.at += 3000; // the model answering
  const held = await pacer.pauseBeforeSend("default", CUSTOMER, "Hello, how may we help you today?");
  assert.ok(held > 0, "the reply is held while it is typed");

  const total = clock.at - arrived;
  assert.ok(total >= 8000 && total <= 40000, `first reply after ${total}ms`);
});

test("a chat already in progress is answered sooner than a cold one", async () => {
  const cold = harness();
  cold.pacer.noteInbound("default", CUSTOMER, "Hi");
  const coldRead = await cold.pacer.pauseBeforeReply("default", CUSTOMER);

  const warm = harness();
  warm.pacer.noteInbound("default", CUSTOMER, "Hi");
  await warm.pacer.pauseBeforeReply("default", CUSTOMER);
  await warm.pacer.pauseBeforeSend("default", CUSTOMER, "Hello");
  warm.clock.at += 20_000;
  warm.pacer.noteInbound("default", CUSTOMER, "ok");
  const warmRead = await warm.pacer.pauseBeforeReply("default", CUSTOMER);

  assert.ok(warmRead >= 2000 && warmRead <= 8500, `active read pause ${warmRead}`);
  assert.ok(warmRead < coldRead);
});

test("a chat left quiet for a while is cold again", async () => {
  const { clock, pacer } = harness();
  pacer.noteInbound("default", CUSTOMER, "Hi");
  await pacer.pauseBeforeReply("default", CUSTOMER);
  await pacer.pauseBeforeSend("default", CUSTOMER, "Hello");
  clock.at += 6 * 60 * 1000;
  pacer.noteInbound("default", CUSTOMER, "Hi");

  assert.ok((await pacer.pauseBeforeReply("default", CUSTOMER)) >= 5000);
});

test("a longer reply is held longer, within bounds", async () => {
  async function holdFor(content) {
    const { pacer } = harness();
    pacer.noteInbound("default", CUSTOMER, "price?");
    await pacer.pauseBeforeReply("default", CUSTOMER);
    return pacer.pauseBeforeSend("default", CUSTOMER, content);
  }
  const short = await holdFor("Okay");
  const medium = await holdFor("x".repeat(90));
  const huge = await holdFor("x".repeat(5000));

  assert.equal(short, 1500);
  assert.ok(medium > short && medium < huge);
  assert.equal(huge, 30000);
});

test("time the model already took counts toward typing", async () => {
  const { clock, pacer } = harness();
  pacer.noteInbound("default", CUSTOMER, "price?");
  await pacer.pauseBeforeReply("default", CUSTOMER);
  clock.at += 60_000;

  assert.equal(await pacer.pauseBeforeSend("default", CUSTOMER, "x".repeat(90)), 0);
});

test("a reply the gate sends without the agent still owes the read pause", async () => {
  const { clock, pacer } = harness();
  const arrived = clock.at;
  pacer.noteInbound("default", CUSTOMER, "John Moyo");

  const held = await pacer.pauseBeforeSend("default", CUSTOMER, "Thanks. What is your phone number?");
  assert.ok(held >= 5000 + 1500, `canned reply held ${held}ms`);
  assert.equal(clock.at - arrived, held);
});

test("the second part of a reply is typed after the first, not sent with it", async () => {
  const { pacer } = harness();
  pacer.noteInbound("default", CUSTOMER, "what do you offer");
  await pacer.pauseBeforeReply("default", CUSTOMER);
  await pacer.pauseBeforeSend("default", CUSTOMER, "x".repeat(60));

  assert.ok((await pacer.pauseBeforeSend("default", CUSTOMER, "x".repeat(60))) >= 1500);
});

test("one line never answers two chats in the same moment", async () => {
  const clock = { at: 1_000_000 };
  const pending = [];
  const pacer = createReplyPacer({
    rng: () => 0.5,
    now: () => clock.at,
    sleep: (ms) => new Promise((resolve) => pending.push({ until: clock.at + ms, resolve })),
  });
  // A backlog: both arrived long enough ago that neither owes any typing time.
  pacer.noteInbound("default", CUSTOMER, "Hi");
  pacer.noteInbound("default", OTHER, "Hi");
  clock.at += 120_000;

  const first = pacer.pauseBeforeSend("default", CUSTOMER, "Hello");
  const second = pacer.pauseBeforeSend("default", OTHER, "Hello");
  assert.equal(await first, 0);
  assert.equal(pending.length, 1, "the second chat waits its turn");
  const gap = pending[0].until - clock.at;
  assert.ok(gap >= 2000 && gap <= 6000, `gap between chats ${gap}`);
  pending[0].resolve();
  assert.equal(await second, gap);
});

test("the two lines are paced independently", async () => {
  const { clock, pacer } = harness();
  pacer.noteInbound("default", CUSTOMER, "Hi");
  pacer.noteInbound("ads", OTHER, "Hi");
  clock.at += 120_000;

  assert.equal(await pacer.pauseBeforeSend("default", CUSTOMER, "Hello"), 0);
  assert.equal(await pacer.pauseBeforeSend("ads", OTHER, "Hello"), 0);
});

test("no reply is held past the cap, however long the queue", async () => {
  const { clock, pacer } = harness();
  const senders = Array.from({ length: 60 }, (_, i) => `+2637700000${String(i).padStart(2, "0")}`);
  for (const sender of senders) pacer.noteInbound("default", sender, "Hi");
  clock.at += 120_000;
  const at = clock.at;

  // All sixty are queued at the same moment: the clock is rewound before each.
  const waits = [];
  for (const sender of senders) {
    clock.at = at;
    waits.push(await pacer.pauseBeforeSend("default", sender, "Hello"));
  }
  assert.ok(Math.max(...waits) <= MAX_HOLD_MS);
  assert.equal(waits.at(-1), MAX_HOLD_MS);
});

test("a send with no recent customer message is never held", async () => {
  const { clock, sleeps, pacer } = harness();

  // A broadcast, an alert, a CV delivery: nobody wrote first.
  assert.equal(await pacer.pauseBeforeSend("default", CUSTOMER, "Daily advert"), null);
  assert.equal(await pacer.pauseBeforeSend("default", "120363407177302513@g.us", "Daily advert"), null);
  assert.equal(await pacer.pauseBeforeReply("default", CUSTOMER), null);

  pacer.noteInbound("default", CUSTOMER, "Hi");
  clock.at += 11 * 60 * 1000;
  assert.equal(await pacer.pauseBeforeSend("default", CUSTOMER, "Your CV is ready"), null);
  // The inbound was on the other line.
  pacer.noteInbound("ads", CUSTOMER, "Hi");
  assert.equal(await pacer.pauseBeforeSend("default", CUSTOMER, "Hello"), null);
  assert.deepEqual(sleeps, []);
});

test("the same chat is found whichever way WhatsApp spells the number", async () => {
  assert.equal(peerKey("+263 77 123 4567"), "263771234567");
  assert.equal(peerKey("263771234567@s.whatsapp.net"), "263771234567");
  assert.equal(peerKey("263771234567:12@s.whatsapp.net"), "263771234567");
  assert.equal(peerKey("84712345678901@lid"), "84712345678901@lid");
  assert.equal(peerKey(""), "");

  const { pacer } = harness();
  pacer.noteInbound("default", "263771234567@s.whatsapp.net", "Hi");
  assert.notEqual(await pacer.pauseBeforeSend("default", CUSTOMER, "Hello"), null);
});

test("the pause before the agent is taken once per message", async () => {
  const { pacer } = harness();
  pacer.noteInbound("default", CUSTOMER, "Hi");

  assert.ok((await pacer.pauseBeforeReply("default", CUSTOMER)) > 0);
  assert.equal(await pacer.pauseBeforeReply("default", CUSTOMER), 0);
});

test("pacing is on unless the kill switch says otherwise", () => {
  const before = process.env[REPLY_PACING_ENV];
  try {
    delete process.env[REPLY_PACING_ENV];
    assert.equal(isReplyPacingEnabled(), true);
    process.env[REPLY_PACING_ENV] = "0";
    assert.equal(isReplyPacingEnabled(), false);
    process.env[REPLY_PACING_ENV] = "1";
    assert.equal(isReplyPacingEnabled(), true);
  } finally {
    if (before === undefined) delete process.env[REPLY_PACING_ENV];
    else process.env[REPLY_PACING_ENV] = before;
  }
});
