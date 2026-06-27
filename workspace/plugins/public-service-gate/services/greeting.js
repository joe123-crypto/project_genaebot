import fs from "node:fs";

import { expandHome, getPluginConfig } from "../lib/config.js";

const DEFAULT_GREETING_TEMPLATE_PATH = "~/.openclaw/workspace/docs/public-greeting.txt";
const DEFAULT_GREETING = `Hi! I can help with:

1. Webetu Progress meal reservation
2. Personalized news delivery to your inbox
3. Local event scouting around your location
4. Job, scholarship, and internship scouting/application

Which one do you need?`;

const GREETING_PATTERNS = [
  /^(?:hi|hello|hey|hie|yo|salam|salaam|bonjour|good\s+(?:morning|afternoon|evening)|gm|morning|afternoon|evening)[\s!.?]*$/i,
  /^(?:hi|hello|hey|hie|salam|salaam|bonjour)\s+(?:there|openclaw|genaie|service\s+desk|bot|assistant)[\s!.?]*$/i,
  /^(?:start|menu|help|services|service|options)[\s!.?]*$/i,
  /^(?:what\s+(?:can\s+you\s+do|services\s+do\s+you\s+(?:offer|provide))|what\s+do\s+you\s+(?:do|offer|provide))[\s!.?]*$/i,
];

export function isGreetingOrServiceMenuRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 120) return false;
  return GREETING_PATTERNS.some((pattern) => pattern.test(compact));
}

export function getServiceMenuSelection(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  const match = /^([1-4])[\s.)-]*$/.exec(compact);
  return match ? match[1] : "";
}

export function isStateResetRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  return /^(?:menu|main\s+menu|services|service|options|help|start\s+over|restart|cancel)[\s!.?]*$/i.test(compact);
}

export function loadGreeting(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.greetingTemplatePath === "string" && pluginCfg.greetingTemplatePath.trim()
      ? pluginCfg.greetingTemplatePath.trim()
      : DEFAULT_GREETING_TEMPLATE_PATH;
  try {
    const text = fs.readFileSync(expandHome(configuredPath), "utf8").trim();
    return text || DEFAULT_GREETING;
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to read greeting template (${String(err)})`);
    return DEFAULT_GREETING;
  }
}
