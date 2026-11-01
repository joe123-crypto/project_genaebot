import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expandHome, getPluginConfig } from "../lib/config.js";
import { getServiceStatePath } from "../lib/state.js";

const DATA = fileURLToPath(new URL("../../../data/", import.meta.url));
export const BOREHOLE_SERVICES = ["survey", "drilling", "casing", "installation", "drilling_and_installation", "flushing", "deepening", "capacity_testing", "repair", "pump_retrieval"];
const TTL = 24 * 60 * 60 * 1000;
const normalize = (s) => String(s || "").normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

export function loadCatalog(api) {
  const config = getPluginConfig(api);
  const data = JSON.parse(fs.readFileSync(expandHome(config.boreholeCatalogPath || path.join(DATA, "borehole-providers.json")), "utf8"));
  const towns = JSON.parse(fs.readFileSync(path.join(DATA, "zimbabwe-towns.json"), "utf8")).towns;
  if (data.version !== 1 || !Array.isArray(data.providers) || !data.providers.length || !Array.isArray(towns)) throw new Error("Invalid borehole catalog");
  const ids = new Set();
  for (const p of data.providers) {
    if (!p.id || ids.has(p.id) || !p.name || !resolveTown(p.base, towns) || !p.coverage || !Array.isArray(p.offers) || !Array.isArray(p.conditions)) throw new Error("Invalid provider");
    ids.add(p.id);
    for (const o of p.offers) {
      if (!BOREHOLE_SERVICES.includes(o.service) || o.currency !== "USD" || !(o.price === null || (Number.isFinite(o.price) && o.price >= 0))) throw new Error("Invalid offer");
    }
  }
  return { providers: data.providers, towns };
}

// Exact aliases only: an unrecognized village must not silently match another town.
export function resolveTown(location, towns) {
  const value = normalize(location);
  if (!value) return null;
  const found = towns.filter((t) => [t.name, ...(t.aliases || [])].some((s) => normalize(s) === value));
  return found.length === 1 ? found[0] : null;
}

function distance(a, b) {
  const rad = (x) => x * Math.PI / 180;
  const x = Math.sin(rad(b.lat - a.lat) / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(rad(b.lon - a.lon) / 2) ** 2;
  return 6371 * 2 * Math.asin(Math.sqrt(Math.min(1, x)));
}

function statePath(api) { return path.join(path.dirname(getServiceStatePath(api)), "borehole-selections.json"); }
function stateKey(sender, account) { return `${account}:${sender}`; }
function states(api) {
  try { return JSON.parse(fs.readFileSync(statePath(api), "utf8")); } catch { return {}; }
}
export function boreholeSelection(api, sender, account = "default") {
  const state = states(api)[stateKey(sender, account)];
  return state?.expiresAt > Date.now() ? state : null;
}
function save(api, sender, account, selection) {
  const all = Object.fromEntries(Object.entries(states(api)).filter(([, s]) => s?.expiresAt > Date.now()));
  all[stateKey(sender, account)] = { ...selection, expiresAt: Date.now() + TTL };
  const dest = statePath(api);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  const tmp = `${dest}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(all, null, 2) + "\n", { mode: 0o600 });
  fs.renameSync(tmp, dest);
}

function eligibleOffers(provider, request) {
  const services = request.service === "drilling_and_installation" ? ["drilling", "installation"] : [request.service];
  const offers = provider.offers.filter((o) => services.includes(o.service)
    && (o.service !== "installation" || !request.pump_hp || o.pump_hp === request.pump_hp)
    && (o.service !== "casing" || !request.casing_class || o.casing_class === request.casing_class));
  return services.every((s) => offers.some((o) => o.service === s)) ? offers : [];
}
function publicOffer(o, request) {
  const result = { service: o.service, currency: o.currency, price: o.price, unit: o.unit || null };
  for (const k of ["pump_hp", "casing_class", "included_depth_m", "extra_per_m", "inclusions", "conditions"]) if (o[k] !== undefined) result[k] = o[k];
  if (o.service === "drilling" && request.depth_m && o.price !== null && o.included_depth_m && Number.isFinite(o.extra_per_m)) {
    result.drilling_subtotal = o.price + Math.max(0, request.depth_m - o.included_depth_m) * o.extra_per_m;
    result.depth_m = request.depth_m;
    result.subtotal_excludes = "Survey, extra casing, installation and any travel or site-specific charges.";
  }
  return result;
}

export function lookupBorehole(api, sender, params, account = "default") {
  try {
    const { providers, towns } = loadCatalog(api);
    const previous = boreholeSelection(api, sender, account);
    const serviceChanged = params.service && params.service !== previous?.request?.service;
    const request = { ...(previous?.request || {}) };
    if (serviceChanged) for (const k of ["pump_hp", "casing_class", "depth_m"]) delete request[k];
    for (const k of ["location", "service", "pump_hp", "casing_class", "depth_m"]) if (params[k] !== undefined && params[k] !== "") request[k] = params[k];
    // Invalidate a previously selected provider before any clarification or failure.
    save(api, sender, account, { request, providerId: null });
    for (const k of ["pump_hp", "casing_class", "depth_m"]) {
      if (request[k] !== undefined && (!Number.isFinite(request[k]) || request[k] <= 0)) return { status: "needs_requirements", text: "Please confirm the requested pump HP, depth in meters or casing class." };
    }
    const town = resolveTown(request.location, towns);
    if (!town) return { status: "needs_location", text: "Which town or nearest major town in Zimbabwe are you in?" };
    if (!BOREHOLE_SERVICES.includes(request.service)) return { status: "needs_service", text: "Do you need drilling, a survey, solar borehole installation, or another borehole service?" };
    const candidates = providers.flatMap((p) => {
      const covered = p.coverage.nationwide || (p.coverage.towns || []).some((s) => resolveTown(s, towns)?.geoname_id === town.geoname_id);
      const offers = eligibleOffers(p, request);
      return covered && offers.length ? [{ p, offers, distance: distance(town, resolveTown(p.base, towns)) }] : [];
    });
    if (!candidates.length) return { status: "handoff", text: "A person needs to confirm an offer for your location and requirements. Use the borehole handoff action." };
    if (["installation", "drilling_and_installation"].includes(request.service) && !request.pump_hp) {
      const sizes = [...new Set(candidates.flatMap((c) => c.offers.map((o) => o.pump_hp).filter(Number.isFinite)))].sort((a, b) => a - b);
      if (sizes.length) return { status: "needs_requirements", pump_sizes_hp: sizes, text: "What pump size do you need? These are the documented package sizes. If the customer needs sizing advice, use borehole handoff rather than choosing a pump for them." };
    }
    candidates.sort((a, b) => a.distance - b.distance || a.p.id.localeCompare(b.p.id));
    const nearest = candidates.filter((c) => Math.abs(c.distance - candidates[0].distance) < 0.01);
    // Compare only single equivalent priced offers. Generic installation offers
    // with unknown specifications cannot undercut a specified pump package.
    const comparable = nearest.filter((c) => c.offers.length === 1 && c.offers[0].price !== null);
    const scope = (o) => JSON.stringify([o.service, o.currency, o.unit, o.included_depth_m, o.pump_hp, o.casing_class, o.inclusions || []]);
    if (new Set(comparable.map((c) => scope(c.offers[0]))).size > 1) return { status: "handoff", text: "The nearby offers have different inclusions. A person needs to confirm comparable pricing for these requirements; use the borehole handoff action." };
    comparable.sort((a, b) => {
      const price = (c) => publicOffer(c.offers[0], request).drilling_subtotal ?? c.offers[0].price;
      return price(a) - price(b) || a.p.id.localeCompare(b.p.id);
    });
    const selected = comparable[0] || nearest[0];
    save(api, sender, account, { request, providerId: selected.p.id });
    const publicData = {
      status: "ok", offers: selected.offers.map((o) => publicOffer(o, request)),
      conditions: selected.p.conditions,
      guidance: "Answer only from these offers. Do not claim a lowest final total: travel and site conditions may be unconfirmed. Ask for missing package requirements. Use borehole handoff for unknown details or prices. Keep all enquiries through Genaie.",
    };
    // Defense in depth: never release internal identity/base text, even if an
    // operator accidentally copies an original caption into a public field.
    const serialized = JSON.stringify(publicData);
    if (providers.some((p) => [p.name, p.base, p.address].filter(Boolean).some((s) => serialized.toLowerCase().includes(s.toLowerCase())))) throw new Error("Private provider data in public offer");
    return publicData;
  } catch (err) {
    try { save(api, sender, account, { request: {}, providerId: null }); } catch { /* fail closed without blocking handoff */ }
    api.logger?.warn?.("public-service-gate: borehole lookup unavailable");
    return { status: "handoff", text: "Borehole information is unavailable. Do not quote prices or reuse earlier offers; use the borehole handoff action." };
  }
}

export function boreholeHandoffDetails(api, sender, account) {
  const selected = boreholeSelection(api, sender, account);
  if (!selected) return "";
  try {
    const { providers } = loadCatalog(api);
    const provider = providers.find((p) => p.id === selected.providerId);
    return `Borehole enquiry: ${JSON.stringify(selected.request)}. Internal provider: ${provider ? `${provider.name}, ${provider.base}` : "needs confirmation"}.`;
  } catch { return "Borehole provider needs confirmation."; }
}
