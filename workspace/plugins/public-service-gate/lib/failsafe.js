export const FAILSAFE_STOP_PRIORITY = 100;
const FAILSAFE_STOP_SUPPRESSION_TTL_MS = 30 * 60 * 1000;
const failsafeStopSuppressions = new Map();

function stopSuppressionKey(value) {
  const lid = /^(\d+)(?::\d+)?@(lid|hosted\.lid)$/.exec(String(value ?? "").trim());
  if (lid) return `${lid[1]}@${lid[2]}`;
  const digits = String(value ?? "").replace(/[^\d]/g, "");
  return digits || "";
}

export function markFailsafeStopSuppression(sender, now = Date.now()) {
  const key = stopSuppressionKey(sender);
  if (!key) return;
  failsafeStopSuppressions.set(key, now + FAILSAFE_STOP_SUPPRESSION_TTL_MS);
}

export function clearFailsafeStopSuppression(sender) {
  const key = stopSuppressionKey(sender);
  if (!key) return;
  failsafeStopSuppressions.delete(key);
}

export function hasFailsafeStopSuppression(value, now = Date.now()) {
  const key = stopSuppressionKey(value);
  if (!key) return false;
  const expiresAt = failsafeStopSuppressions.get(key);
  if (!expiresAt) return false;
  if (expiresAt <= now) {
    failsafeStopSuppressions.delete(key);
    return false;
  }
  return true;
}

export function isFailsafeStopCommand(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  return /^stop[!.?]*$/i.test(compact);
}
