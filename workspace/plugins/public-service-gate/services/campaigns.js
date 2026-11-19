import { fileURLToPath } from "node:url";
import { getPluginConfig } from "../lib/config.js";
import { helperLimiter, runJsonCommand } from "../lib/runtime.js";
import { isCvIntakePaused } from "./cv-maker.js";

const reader = fileURLToPath(new URL("../../../bin/public-read", import.meta.url));
const unavailable = {
  status: "unavailable", campaigns: [],
  guidance: "Current campaign information is unavailable. Use handoff; never reuse facts from history or the retired briefing.",
};
const identifier = /^[a-z0-9][a-z0-9-]{0,63}$/;
const onlyKeys = (value, allowed) => value && typeof value === "object" && !Array.isArray(value)
  && Object.keys(value).every((key) => allowed.includes(key));
const shortText = (value, max) => typeof value === "string" && value.trim().length > 0 && value.length <= max;

function validSchedule(row) {
  return onlyKeys(row, ["time", "timezone", "weekdays", "paused", "skipped_today", "pending"])
    && typeof row.time === "string" && /^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(row.time)
    && shortText(row.timezone, 120) && Array.isArray(row.weekdays) && row.weekdays.length > 0 && row.weekdays.length <= 7
    && row.weekdays.every((day) => Number.isInteger(day) && day >= 0 && day <= 6)
    && [row.paused, row.skipped_today, row.pending].every((flag) => typeof flag === "boolean");
}

function validCampaign(row) {
  if (!onlyKeys(row, ["campaign_id", "name", "advertising_status", "requires_action", "advertised_content", "schedule"])
      || !shortText(row.name, 120) || !["enabled", "disabled", "paused", "pending"].includes(row.advertising_status)) return false;
  if (row.requires_action === "borehole_info") {
    if (row.name !== "Borehole services" || row.campaign_id !== undefined || row.advertised_content !== undefined || row.schedule !== undefined) return false;
  } else if (row.requires_action !== undefined || typeof row.campaign_id !== "string" || !identifier.test(row.campaign_id)) return false;
  if (row.advertising_status !== "enabled") return row.advertised_content === undefined && row.schedule === undefined;
  if (row.schedule === undefined && row.advertised_content === undefined) return true;
  if (!Array.isArray(row.schedule) || row.schedule.length > 20 || !row.schedule.every((entry) => validSchedule(entry) && !entry.pending)) return false;
  return Array.isArray(row.advertised_content)
    && row.advertised_content.every((text) => shortText(text, 16 * 1024))
    && Buffer.byteLength(JSON.stringify(row.advertised_content)) <= 16 * 1024;
}

function validResult(result, cvAvailable) {
  return onlyKeys(result, ["status", "source", "guidance", "service_availability", "fetched_at", "campaigns", "more"])
    && ["ok", "needs_clarification", "not_found"].includes(result.status) && result.source === "live_campaigns"
    && shortText(result.guidance, 2000) && shortText(result.fetched_at, 64) && Number.isFinite(Date.parse(result.fetched_at))
    && typeof result.more === "boolean" && Array.isArray(result.campaigns) && result.campaigns.length <= 20
    && onlyKeys(result.service_availability, ["cv_new_orders"]) && result.service_availability.cv_new_orders === cvAvailable
    && (result.status === "not_found" ? result.campaigns.length === 0 : result.campaigns.length > 0)
    && result.campaigns.every(validCampaign);
}

export async function lookupCampaigns(api, params = {}, run = runJsonCommand) {
  const cvAvailable = !isCvIntakePaused() && getPluginConfig(api).cvMakerEnabled === true;
  if ((params.query !== undefined && (typeof params.query !== "string" || params.query.length > 200))
      || (params.campaign_id !== undefined && (typeof params.campaign_id !== "string" || !identifier.test(params.campaign_id)))
      || (params.query && params.campaign_id)) return unavailable;
  const args = [reader];
  if (params.discover) args.push("--discover");
  if (params.query) args.push(`--query=${params.query}`);
  if (params.campaign_id) args.push(`--campaign-id=${params.campaign_id}`);
  if (cvAvailable) args.push("--cv-available");
  try {
    const result = await helperLimiter(() => run("python3", args, { timeoutMs: 30_000, maxBuffer: 128 * 1024 }));
    if (result?.status === "unavailable") return unavailable;
    // This is a public boundary, not just a JSON parser. Reject accidental
    // operator fields, malformed nested values and stale offers on inactive rows.
    if (!validResult(result, cvAvailable)) throw new Error("Invalid public lookup");
    return result;
  } catch {
    api.logger?.warn?.("public-service-gate: live campaign lookup unavailable");
    return unavailable;
  }
}
