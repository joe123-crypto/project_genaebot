import assert from "node:assert/strict";
import test from "node:test";

import { containsDash, stripDashes } from "./style.js";

test("a spaced em dash becomes the comma a person would have typed", () => {
  assert.equal(
    stripDashes("We are based in Bindura, but we're mobile — we come to you once you place an order."),
    "We are based in Bindura, but we're mobile, we come to you once you place an order.",
  );
});

test("no outbound text keeps an em, en or horizontal bar dash", () => {
  for (const dash of ["\u2012", "\u2013", "\u2014", "\u2015", "\u2E3A", "\u2E3B"]) {
    const scrubbed = stripDashes(`Water survey ${dash} $100`);
    assert.equal(containsDash(scrubbed), false, `${dash.codePointAt(0).toString(16)} survived`);
  }
});

test("a dash between numbers reads as a range", () => {
  assert.equal(stripDashes("Solar installation is $1800 — $2400."), "Solar installation is $1800 to $2400.");
  assert.equal(stripDashes("40–60m"), "40 to 60m");
  assert.equal(stripDashes("20–30–40"), "20 to 30 to 40");
});

test("a dash already following punctuation does not double it", () => {
  assert.equal(stripDashes("Yes, — we deliver."), "Yes, we deliver.");
});

test("a word-internal dash becomes a hyphen and other unspaced dashes a gap", () => {
  assert.equal(stripDashes("pre–paid"), "pre-paid");
  assert.equal(stripDashes("Total—$1,450 owed"), "Total $1,450 owed");
});

test("a dash opening or closing a line is dropped with its padding", () => {
  assert.equal(stripDashes("Prices:\n— Water survey: $100"), "Prices:\nWater survey: $100");
  assert.equal(stripDashes("Drilling —\nfirst 40m: $950"), "Drilling\nfirst 40m: $950");
});

test("line structure and single spaces survive the rewrite", () => {
  assert.equal(
    stripDashes("Pa 60m:\n\n* First 40m: $950\n* Extra 20m — $500"),
    "Pa 60m:\n\n* First 40m: $950\n* Extra 20m, $500",
  );
});

test("a URL is handed back character for character", () => {
  const link = "https://agent.genaie.site/setup?token=aB3-x_Y9—keep";
  const text = `Sign in here — ${link} and tell me when you're in.`;
  const scrubbed = stripDashes(text);
  assert.ok(scrubbed.includes(link), scrubbed);
  assert.equal(scrubbed, `Sign in here, ${link} and tell me when you're in.`);
});

test("text without a dash is returned untouched", () => {
  const clean = "Ndatenda, tiripo.\n\nPa 60m:\n* First 40m: $950";
  assert.equal(stripDashes(clean), clean);
  assert.equal(containsDash(clean), false);
});

test("a dash-only reply is never scrubbed down to nothing", () => {
  assert.equal(stripDashes("—"), "—");
  assert.equal(stripDashes(""), "");
  assert.equal(stripDashes(undefined), "");
});
