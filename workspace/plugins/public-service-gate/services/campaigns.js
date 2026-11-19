import { fileURLToPath } from "node:url";
import { getPluginConfig } from "../lib/config.js";
import { helperLimiter, runJsonCommand } from "../lib/runtime.js";
import { isCvIntakePaused } from "./cv-maker.js";

const reader = fileURLToPath(new URL("../../../bin/public-read", import.meta.url));
const unavailable = {
  status: "unavailable", campaigns: [],
  guidance: "Current campaign information is unavailable. Use handoff; never reuse facts from history or the retired briefing.",
};

export async function lookupCampaigns(api, params = {}, run = runJsonCommand) {
  const cvAvailable = !isCvIntakePaused() && getPluginConfig(api).cvMakerEnabled === true;
  if ((params.query !== undefined && (typeof params.query !== "string" || params.query.length > 200))
      || (params.campaign_id !== undefined && (typeof params.campaign_id !== "string" || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(params.campaign_id)))
      || (params.query && params.campaign_id)) return unavailable;
  const args = [reader];
  if (params.discover) args.push("--discover");
  if (params.query) args.push(`--query=${params.query}`);
  if (params.campaign_id) args.push(`--campaign-id=${params.campaign_id}`);
  if (cvAvailable) args.push("--cv-available");
  try {
    const result = await helperLimiter(() => run("python3", args, { timeoutMs: 30_000, maxBuffer: 128 * 1024 }));
    if (!["ok", "needs_clarification", "not_found", "unavailable"].includes(result?.status)
        || !Array.isArray(result.campaigns)) throw new Error("Invalid public lookup");
    return result;
  } catch {
    api.logger?.warn?.("public-service-gate: live campaign lookup unavailable");
    return unavailable;
  }
}
