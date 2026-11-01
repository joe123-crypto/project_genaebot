import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { boreholeHandoffDetails, boreholeSelection, loadCatalog, lookupBorehole, resolveTown } from "./borehole.js";

function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "borehole-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const config = { serviceStatePath: path.join(dir, "state.json") };
  const api = { config: { plugins: { entries: { "public-service-gate": { config } } } }, logger: { warn() {} } };
  return { dir, config, api, lookup: (p, account = "default", sender = "customer") => lookupBorehole(api, sender, p, account) };
}

test("catalog holds three independent providers and excludes Skylight", (t) => {
  const { api } = fixture(t);
  const { providers, towns } = loadCatalog(api);
  assert.deepEqual(providers.map((p) => [p.name, p.base]), [["Same Age Projects", "Bindura"], ["Big Player", "Harare"], ["Baltic Sea Drilling", "Harare"]]);
  assert.equal(resolveTown("bindura", towns).name, "Bindura");
  assert.equal(resolveTown("unknown village", towns), null);
  assert.equal(resolveTown("ambiguous", [{ name: "A", aliases: ["ambiguous"] }, { name: "B", aliases: ["ambiguous"] }]), null);
});

test("ask location and service before revealing any prices", (t) => {
  const { lookup } = fixture(t);
  assert.equal(lookup({ service: "drilling" }).status, "needs_location");
  assert.equal(lookup({ location: "Harare" }, "ads").status, "needs_service");
  assert.equal(lookup({ location: "Some farm near a village" }).status, "needs_location");
});

test("nearest base takes priority over a cheaper farther offer", (t) => {
  const { api, lookup } = fixture(t);
  const result = lookup({ location: "Bindura", service: "drilling", depth_m: 60 });
  assert.equal(result.offers[0].price, 950);
  assert.equal(result.offers[0].drilling_subtotal, 1450);
  assert.equal(boreholeSelection(api, "customer").providerId, "same-age-projects");
  assert.match(result.offers[0].subtotal_excludes, /casing/);
});

test("Harare drilling uses documented local rates and excludes unknown extras", (t) => {
  const { api, lookup } = fixture(t);
  const result = lookup({ location: "Harare", service: "drilling", depth_m: 60 });
  assert.equal(result.offers[0].drilling_subtotal, 1170);
  assert.equal(boreholeSelection(api, "customer").providerId, "big-player");
  assert.match(result.conditions.join(" "), /travel charges require confirmation/i);
});

test("exact installation package does not borrow an unknown provider's specifications", (t) => {
  const { api, lookup } = fixture(t);
  const clarification = lookup({ location: "Harare", service: "installation" });
  assert.equal(clarification.status, "needs_requirements");
  assert.deepEqual(clarification.pump_sizes_hp, [0.75, 1, 1.5, 2]);
  const result = lookup({ location: "Harare", service: "installation", pump_hp: 1.5 });
  assert.equal(result.offers.length, 1);
  assert.equal(result.offers[0].price, 2200);
  assert.equal(result.offers[0].pump_hp, 1.5);
  assert.match(result.offers[0].inclusions.join(" "), /5000L/);
  assert.equal(boreholeSelection(api, "customer").providerId, "same-age-projects");
  assert.equal(lookup({ pump_hp: 9 }).status, "handoff");
  assert.equal(boreholeSelection(api, "customer").providerId, null);
});

test("unknown price stays null; service unsupported at a location hands off", (t) => {
  const { lookup } = fixture(t);
  const result = lookup({ location: "Harare", service: "capacity_testing" });
  assert.equal(result.offers[0].price, null);
  assert.equal(lookup({ location: "Victoria Falls" }).status, "handoff");
});

test("state reuses location, updates on changes, and isolates accounts and senders", (t) => {
  const { api, lookup } = fixture(t);
  lookup({ location: "Bindura", service: "installation", pump_hp: 1 });
  assert.equal(lookup({}).offers[0].price, 2000);
  const changed = lookup({ location: "Harare", service: "drilling", depth_m: 60 });
  assert.equal(changed.offers[0].drilling_subtotal, 1170);
  assert.equal(boreholeSelection(api, "customer").request.pump_hp, undefined);
  assert.equal(lookup({}, "ads").status, "needs_location");
  assert.equal(lookup({}, "default", "someone-else").status, "needs_location");
  lookup({ location: "Unknown" });
  assert.equal(boreholeSelection(api, "customer").providerId, null);
});

test("expired selection does not leak into a new enquiry", (t) => {
  const { api, dir, lookup } = fixture(t);
  lookup({ location: "Bindura", service: "survey" });
  const p = path.join(dir, "borehole-selections.json");
  const state = JSON.parse(fs.readFileSync(p));
  state["default:customer"].expiresAt = 0;
  fs.writeFileSync(p, JSON.stringify(state));
  assert.equal(boreholeSelection(api, "customer"), null);
  assert.equal(lookup({}).status, "needs_location");
});

test("customer output omits provider identity and bases; operator context retains selection", (t) => {
  const { api, lookup } = fixture(t);
  const result = lookup({ location: "Harare", service: "drilling" });
  assert.doesNotMatch(JSON.stringify(result), /Same Age|Big Player|Baltic|Skylight|Harare|Bindura|Western|providerId|source_caption/i);
  assert.match(boreholeHandoffDetails(api, "customer", "default"), /Big Player, Harare/);
});

test("catalog failures and accidentally published private identity fail closed", (t) => {
  const { api, config, dir, lookup } = fixture(t);
  const data = loadCatalog(api);
  config.boreholeCatalogPath = path.join(dir, "catalog.json");
  fs.writeFileSync(config.boreholeCatalogPath, JSON.stringify({ version: 1, providers: data.providers.map((p) => ({ ...p, conditions: [p.name] })) }));
  assert.equal(lookup({ location: "Harare", service: "drilling" }).status, "handoff");
  fs.writeFileSync(config.boreholeCatalogPath, "invalid");
  assert.equal(lookup({}).status, "handoff");
  assert.equal(boreholeSelection(api, "customer").providerId, null);
});

test("among equivalent nearby offers the lower known price wins, unknown is never zero", (t) => {
  const { api, config, dir, lookup } = fixture(t);
  const { providers } = loadCatalog(api);
  const local = providers.filter((p) => p.base === "Harare");
  for (const p of local) p.offers = [{ service: "survey", currency: "USD", price: p.id === "big-player" ? 90 : 80, unit: "survey" }];
  config.boreholeCatalogPath = path.join(dir, "catalog.json");
  fs.writeFileSync(config.boreholeCatalogPath, JSON.stringify({ version: 1, providers: local }));
  assert.equal(lookup({ location: "Harare", service: "survey" }).offers[0].price, 80);
  local[1].offers[0].price = null;
  fs.writeFileSync(config.boreholeCatalogPath, JSON.stringify({ version: 1, providers: local }));
  assert.equal(lookup({}).offers[0].price, 90);
  local[1].offers[0] = { ...local[1].offers[0], price: 70, inclusions: ["Different scope"] };
  fs.writeFileSync(config.boreholeCatalogPath, JSON.stringify({ version: 1, providers: local }));
  assert.equal(lookup({}).status, "handoff");
});

test("invalid numeric requirements do not generate a quote", (t) => {
  const { lookup } = fixture(t);
  assert.equal(lookup({ location: "Bindura", service: "drilling", depth_m: -1 }).status, "needs_requirements");
});
