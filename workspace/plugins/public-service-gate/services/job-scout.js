import { expandHome, getPluginConfig } from "../lib/config.js";
import {
  HELPER_TIMEOUT_MS,
  busyReply,
  helperLimiter,
  isQueueFullError,
  runJsonCommand,
  sanitizeLogText,
} from "../lib/runtime.js";
import { accountLinkStartOnboarding, getAccountLinkScript } from "../lib/account-link.js";
import { clearServiceState, loadServiceState, saveServiceState } from "../lib/state.js";

const DEFAULT_JOB_SCOUT_SCRIPT = "~/.openclaw/workspace/bin/job-scout";
const DEFAULT_JOB_SCOUT_SETUP_FAILED = `I couldn't start Job Scout. Try again later.`;
const JOB_SCOUT_UNAVAILABLE = "Job Scout setup is temporarily unavailable. Try again later.";

const JOB_SCOUT_REQUEST_PATTERNS = [
  /\bjob\s*(?:scout|scouting|application|applications?)\b/i,
  /\b(?:apply|applying)\b.*\bjobs?\b/i,
  /\b(?:find|look\s+for|search\s+for|scout)\b.*\b(?:jobs?|internships?|scholarships?)\b/i,
  /\b(?:jobs?|internships?|scholarships?)\b.*\b(?:find|search|scout|apply|application|applications?|register|subscribe|start|setup|set\s*up)\b/i,
];
// Explicit intent to set up / onboard job scouting. Only a job-scouting message
// that ALSO carries one of these keywords gets the canned setup link; every
// other jobs-related message (questions, "find/scout/apply" phrasings, anything
// ambiguous) falls through to the relevance gate -> LLM.
const JOB_SCOUT_SETUP_PATTERNS = [
  /\b(?:set\s*up|setup)\b/i,
  /\bonboard(?:ing|ed)?\b/i,
];

function getJobScoutScript(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.jobScoutScript === "string" && pluginCfg.jobScoutScript.trim()
      ? pluginCfg.jobScoutScript.trim()
      : DEFAULT_JOB_SCOUT_SCRIPT;
  return expandHome(configuredPath);
}

function logJobScoutFailure(api, phase, script, err) {
  const status = err?.status ?? err?.code;
  const statusPart = status === undefined ? "" : ` status=${status}`;
  const message = sanitizeLogText(err?.message ?? String(err));
  const stderr = sanitizeLogText(err?.stderr);
  const stderrPart = stderr ? ` stderr="${stderr}"` : "";
  api.logger?.warn?.(
    `public-service-gate: job scout failure phase="${phase}" script=${script}${statusPart} error="${message}"${stderrPart}`,
  );
  if (err && typeof err === "object") {
    err.publicServiceGateLogged = true;
  }
}

export function isJobScoutRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 420) return false;
  return JOB_SCOUT_REQUEST_PATTERNS.some((pattern) => pattern.test(compact));
}

// Only an explicit "set up" / "onboard" job-scouting message is claimed
// deterministically. Anything else about jobs falls through to the LLM, so we
// no longer hijack questions like "can I change the jobs you scout?".
export function isJobScoutSetupCommand(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 420) return false;
  if (!JOB_SCOUT_REQUEST_PATTERNS.some((pattern) => pattern.test(compact))) return false;
  return JOB_SCOUT_SETUP_PATTERNS.some((pattern) => pattern.test(compact));
}

function jobScoutChannelQuestion() {
  return `How do you want to set up Job Scout?

1. In the web app
2. Here in WhatsApp`;
}

function selectedChannel(text) {
  const compact = String(text ?? "").trim().toLowerCase().replace(/\s+/g, " ");
  if (!compact) return null;
  const web = /^(?:1|web|web app|website|browser)[.!]?$/.test(compact)
    || /\b(?:choose|pick|use|want|prefer|continue|finish|set\s*up)(?:\s+(?:it|this|setup))?\s+(?:on|in|with|using|through)?\s*(?:the\s+)?(?:web|web app|website|browser)\b/.test(compact);
  const chat = /^(?:2|chat|whatsapp|here|in chat)[.!]?$/.test(compact)
    || /\b(?:choose|pick|use|want|prefer|continue|finish|set\s*up)(?:\s+(?:it|this|setup))?\s+(?:on|in|with|using|through)?\s*(?:this\s+)?(?:chat|whatsapp|here)\b/.test(compact);
  if (web === chat) return null;
  return web ? "web" : "chat";
}

function jobScoutStatus(api, sender) {
  return helperLimiter(() => runJsonCommand(
    getJobScoutScript(api),
    ["status", "--phone", sender],
    { timeoutMs: HELPER_TIMEOUT_MS },
  ));
}

function selectedChannelReply(channel, result) {
  const url = String(result?.setupUrl || result?.nextUrl || "").trim();
  if (channel === "web") {
    if (!url) return "Your Job Scout setup is ready.";
    return `Finish setting up Job Scout in the web app:
${url}`;
  }
  if (result?.nextStep === "dashboard") return "Your Job Scout setup is ready.";
  if (result?.nextStep === "whatsapp_chat") {
    return "You're ready to continue here. Do you want guided questions or CV autofill?";
  }
  if (!url) return "Continue your Job Scout setup here in WhatsApp.";
  return `Sign in to connect your account here:
${url}

Gmail is granted as part of sign-in. The app will bring you back here automatically.`;
}

export async function handleJobScoutRequest(api, sender, text = "") {
  if (getPluginConfig(api).jobScoutOnboardingEnabled !== true) {
    return { handled: true, text: JOB_SCOUT_UNAVAILABLE };
  }
  try {
    let status;
    try {
      status = await jobScoutStatus(api, sender);
    } catch (err) {
      logJobScoutFailure(api, "Job Scout status", getJobScoutScript(api), err);
      throw err;
    }
    if (status.ready === true) {
      clearServiceState(api, sender);
      return { handled: true, text: "Your Job Scout setup is ready." };
    }

    const state = loadServiceState(api, sender);
    const pendingChoice = state?.service === "job-scout" && state?.step === "job-scout.awaiting_setup_channel";
    const explicitChannel = selectedChannel(text);
    const channel = explicitChannel || (pendingChoice ? selectedChannel(text) : null) || status.onboardingChannel || null;
    if (!channel) {
      saveServiceState(api, sender, "job-scout", "job-scout.awaiting_setup_channel");
      return { handled: true, text: jobScoutChannelQuestion() };
    }

    let result;
    try {
      result = await accountLinkStartOnboarding(api, sender, "jobs", channel);
    } catch (err) {
      logJobScoutFailure(api, "onboarding start", getAccountLinkScript(api), err);
      throw err;
    }
    clearServiceState(api, sender);
    return { handled: true, text: selectedChannelReply(channel, result) };
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    if (!err?.publicServiceGateLogged) {
      logJobScoutFailure(api, "Job Scout invite creation", getJobScoutScript(api), err);
    }
    return {
      handled: true,
      text: DEFAULT_JOB_SCOUT_SETUP_FAILED,
    };
  }
}
