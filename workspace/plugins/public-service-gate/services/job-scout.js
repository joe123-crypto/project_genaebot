import { expandHome, getPluginConfig } from "../lib/config.js";
import { HELPER_TIMEOUT_MS, busyReply, helperLimiter, isQueueFullError, runJsonCommand, sanitizeLogText } from "../lib/runtime.js";
import {
  accountLinkInvite,
  accountLinkRequiredText,
  accountLinkStatus,
  getAccountLinkScript,
  scopedAppUrl,
} from "../lib/account-link.js";

const DEFAULT_JOB_SCOUT_SCRIPT = "~/.openclaw/workspace/bin/job-scout";
const DEFAULT_JOB_SCOUT_SETUP_FAILED = `I couldn't start Job Scout setup right now. Please try again later.`;

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

function runJobScoutScript(api, args) {
  const script = getJobScoutScript(api);
  return helperLimiter(() => runJsonCommand(script, args, { timeoutMs: HELPER_TIMEOUT_MS }));
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

function jobScoutStartText(setupUrl) {
  return `Job Scout is active.

Open this setup link and sign in with your email:
${setupUrl}

After sign-in, the app will open Gmail connection automatically.

Then choose one setup option here:
1. Answer a few short questions.
2. Upload your CV and allow me to pull the details from it.

I will ask only one or two questions at a time.`;
}

export async function handleJobScoutRequest(api, sender) {
  try {
    const accountLinkScript = getAccountLinkScript(api);
    const jobScoutScript = getJobScoutScript(api);
    let status;
    try {
      status = await accountLinkStatus(api, sender);
    } catch (err) {
      logJobScoutFailure(api, "account-link status", accountLinkScript, err);
      throw err;
    }
    if (!status.linked) {
      let linkInvite;
      try {
        linkInvite = await accountLinkInvite(api, sender, "jobs", "/connect-gmail");
        if (!linkInvite.setupUrl) throw new Error("missing setupUrl");
      } catch (err) {
        logJobScoutFailure(api, "account-link invite", accountLinkScript, err);
        throw err;
      }
      return {
        handled: true,
        text: accountLinkRequiredText(linkInvite.setupUrl, "Job Scout"),
      };
    }
    let invite;
    try {
      invite = await runJobScoutScript(api, ["create-invite", "--phone", sender]);
    } catch (err) {
      logJobScoutFailure(api, "Job Scout invite creation", jobScoutScript, err);
      throw err;
    }
    const setupUrl = invite.setupUrl || scopedAppUrl(api, status.publicUserId, "/connect-gmail");
    return {
      handled: true,
      text: jobScoutStartText(setupUrl),
    };
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
