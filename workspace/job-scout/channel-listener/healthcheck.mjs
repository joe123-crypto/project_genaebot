import fs from "node:fs";

const path = process.env.JOB_SCOUT_CHANNEL_HEALTH || "/data/health.json";
const maxAgeMs = Number(process.env.JOB_SCOUT_CHANNEL_HEALTH_MAX_AGE_MS || 120000);
try {
  const health = JSON.parse(fs.readFileSync(path, "utf8"));
  const age = Date.now() - Date.parse(health.lastHeartbeatAt || "");
  if (!health.connected || !health.authenticated || !Number.isFinite(age) || age > maxAgeMs) process.exit(1);
  process.exit(0);
} catch {
  process.exit(1);
}
