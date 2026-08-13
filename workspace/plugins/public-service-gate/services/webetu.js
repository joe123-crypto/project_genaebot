import { getPluginConfig } from "../lib/config.js";
import {
  WEBETU_TIMEOUT_MS,
  busyReply,
  isQueueFullError,
  runJsonCommand,
  sanitizeLogText,
  textFromErrorField,
  webetuLimiter,
} from "../lib/runtime.js";
import { clearServiceState, saveServiceState } from "../lib/state.js";
import {
  accountLinkInvite,
  accountLinkStatus,
  hasPendingWebetuVaultLink,
  scopedAppUrl,
  withVerbatimLinkDirective,
} from "../lib/account-link.js";
import { getServiceMenuSelection } from "./greeting.js";

const DEFAULT_WORKER_SAFE_SCRIPT = "/usr/local/bin/webetu-worker-safe";
const DEFAULT_WORKER_SAFE_USER = "webetu-worker";
const DEFAULT_WORKER_SUDO_COMMAND = "sudo";

const DEFAULT_WEBETU_ONBOARDING_START = `Sign in here and save your Webetu details in the Credentials Vault:
{setupUrl}

Don't send those details here. Check Spam if the sign-in email doesn't arrive.`;
const WEBETU_RESTAURANT_WAIT_REPLY = `Make reservations yourself until your restaurant appears. Message me when it does.`;
const WEBETU_RESTAURANT_USE_SUPPORTED_REPLY = `Send the restaurant name you want from the supported list.`;
const WEBETU_RESTAURANT_CLARIFY_REPLY = `Use a supported restaurant now, or wait for yours to appear?`;
const WEBETU_SETUP_NOT_CONFIRMED = `I couldn't confirm your Webetu setup. Check that your details are saved in the Credentials Vault.`;

const WEBETU_ONBOARDING_PATTERNS = [
  /\bwebetu\b.*\b(?:onboard|onboarding|setup|set\s*up|register|start|activate)\b/i,
  /\b(?:onboard|onboarding|setup|set\s*up|register|start|activate)\b.*\bwebetu\b/i,
  /\bmeal\s+reservation\b.*\b(?:onboard|onboarding|setup|set\s*up|register|start|activate)\b/i,
  /\b(?:onboard|onboarding|setup|set\s*up|register|start|activate)\b.*\bmeal\s+reservation\b/i,
  /^(?:onboard|onboarding)\s+me[\s!.?]*$/i,
];
const WEBETU_GUIDE_PATTERNS = [
  /\b(?:visual|image|picture|screenshot|step\s*by\s*step)\b.*\b(?:guide|instruction|instructions|setup|webetu)\b/i,
  /\b(?:guide|instruction|instructions)\b.*\bwebetu\b/i,
];
const WEBETU_SETUP_DONE_PATTERNS = [
  /^(?:done|finished|complete|completed|saved|i\s+saved\s+it|i'?ve\s+saved\s+it|credentials?\s+saved|saved\s+credentials?)[\s!.?]*$/i,
  /\b(?:done|finished|complete|completed)\b.*\b(?:vault|credential|credentials|webetu)\b/i,
  /\b(?:saved|updated)\b.*\b(?:vault|credential|credentials|webetu)\b/i,
];
// Clear reservation-status phrasings only. Bare "check"/"check again" is left
// out on purpose so it falls through to the LLM, which asks one clarifying
// question (see public-whatsapp-system-prompt.txt).
const RESERVATION_STATUS_PATTERNS = [
  /\b(?:reservation|booking|meal)s?\b.*\bstatus\b/i,
  /\bstatus\b.*\b(?:reservation|booking|meal)s?\b/i,
  /\bcheck\b.*\b(?:reservation|booking|meal)s?\b/i,
  /\b(?:are|is)\b.*\b(?:reservation|booking|meal)s?\b.*\b(?:active|booked|set|done|reserved|ready)\b/i,
  /\bam\s+i\b.*\b(?:booked|reserved)\b/i,
  /\b(?:my|next|upcoming)\b.*\b(?:reservation|booking)\b/i,
  /\bwhen\b.*\b(?:next\s+)?(?:reservation|booking|meal)s?\b/i,
];

export function isWebetuOnboardingStartRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 220) return false;
  if (/@/.test(compact)) return false;
  return WEBETU_ONBOARDING_PATTERNS.some((pattern) => pattern.test(compact));
}

export function isWebetuGuideRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 220) return false;
  return WEBETU_GUIDE_PATTERNS.some((pattern) => pattern.test(compact));
}

export function isWebetuSetupDoneRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 220) return false;
  return WEBETU_SETUP_DONE_PATTERNS.some((pattern) => pattern.test(compact));
}

export function isReservationStatusRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 220) return false;
  return RESERVATION_STATUS_PATTERNS.some((pattern) => pattern.test(compact));
}

function isWebetuWaitChoice(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  return (
    getServiceMenuSelection(compact) === "2" ||
    /\b(?:wait|skip|later|manual|manually|not\s+now|do\s+not\s+reserve|don't\s+reserve)\b/i.test(compact)
  );
}

function isWebetuUseSupportedChoice(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  return (
    getServiceMenuSelection(compact) === "1" ||
    /\b(?:use|choose|supported|default|set\s+it\s+up|reserve\s+now)\b/i.test(compact)
  );
}

function shouldClarifyWebetuRestaurantChoice(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact) return true;
  if (/^[3-4][\s.)-]*$/.test(compact)) return true;
  return /^(?:yes|no|ok|okay|sure|thanks?|thank\s+you)[\s!.?]*$/i.test(compact);
}

function loadWebetuOnboardingStart(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.webetuOnboardingStartText === "string" && pluginCfg.webetuOnboardingStartText.trim()
    ? pluginCfg.webetuOnboardingStartText.trim()
    : DEFAULT_WEBETU_ONBOARDING_START;
}

function webetuAccountLinkStart(api, setupUrl) {
  return withVerbatimLinkDirective(loadWebetuOnboardingStart(api).replaceAll("{setupUrl}", setupUrl));
}

function webetuVaultReadyText(api, publicUserId) {
  return `Save your Webetu details here, then message me:
${scopedAppUrl(api, publicUserId, "/vault")}

Don't send those details in this chat.`;
}

function getWorkerSafeScript(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.workerSafeScript === "string" && pluginCfg.workerSafeScript.trim()
    ? pluginCfg.workerSafeScript.trim()
    : DEFAULT_WORKER_SAFE_SCRIPT;
}

function getWorkerSafeUser(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.workerSafeUser === "string" && pluginCfg.workerSafeUser.trim()
    ? pluginCfg.workerSafeUser.trim()
    : DEFAULT_WORKER_SAFE_USER;
}

function getWorkerSudoCommand(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.workerSudoCommand === "string" && pluginCfg.workerSudoCommand.trim()
    ? pluginCfg.workerSudoCommand.trim()
    : DEFAULT_WORKER_SUDO_COMMAND;
}

function runWorkerSafe(api, args) {
  return webetuLimiter(() =>
    runJsonCommand(getWorkerSudoCommand(api), ["-n", "-u", getWorkerSafeUser(api), getWorkerSafeScript(api), ...args], {
      timeoutMs: WEBETU_TIMEOUT_MS,
    }),
  );
}

// lookup-user exits non-zero with a JSON body on "not_found"; recover it.
async function workerLookupUser(api, phone) {
  try {
    return await runWorkerSafe(api, ["lookup-user", "--phone", phone]);
  } catch (err) {
    const stdout = textFromErrorField(err?.stdout).trim();
    if (stdout) {
      try {
        return JSON.parse(stdout);
      } catch {
        /* fall through to rethrow */
      }
    }
    throw err;
  }
}

function logWorkerFailure(api, phase, err) {
  const status = err?.status ?? err?.code;
  const statusPart = status === undefined ? "" : ` status=${status}`;
  const message = sanitizeLogText(err?.message ?? String(err));
  const stderr = sanitizeLogText(err?.stderr);
  const stderrPart = stderr ? ` stderr="${stderr}"` : "";
  api.logger?.warn?.(
    `public-service-gate: worker failure phase="${phase}"${statusPart} error="${message}"${stderrPart}`,
  );
}

function formatReservationDate(value) {
  const raw = String(value ?? "").trim();
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
  if (!match) return raw || "the next available day";
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleDateString("en-GB", { day: "numeric", month: "long", timeZone: "UTC" });
}

// Build a sanitized, plain-language status line. Never expose user_id, source,
// status codes, meal-type internals, or raw JSON.
function formatReservationStatusReply(result) {
  const restaurant = String(result?.restaurant ?? "").trim() || "your restaurant";
  const when = formatReservationDate(result?.date);
  const status = String(result?.status ?? "");
  if (status === "already_booked") {
    return `Your meals for ${when} are already reserved at ${restaurant}.`;
  }
  const alreadyBooked = Number(result?.meals_reserved) || 0;
  if (alreadyBooked <= 0) {
    return `Your meals for ${when} at ${restaurant} aren't reserved yet. Please make the reservation manually.`;
  }
  return `Some meals for ${when} at ${restaurant} are already reserved. Please make any remaining reservations manually.`;
}

// Same redirect the onboarding-start branch uses, for users who ask for status
// before they are set up.
async function webetuOnboardingRedirectText(api, phone) {
  const status = await accountLinkStatus(api, phone);
  if (status.linked) return webetuVaultReadyText(api, status.publicUserId);
  const invite = await accountLinkInvite(api, phone, "webetu", "/vault");
  if (!invite.setupUrl) throw new Error("missing setupUrl");
  return webetuAccountLinkStart(api, invite.setupUrl);
}

export async function handleWebetuOnboardingStart(api, sender) {
  try {
    const status = await accountLinkStatus(api, sender);
    if (status.linked) {
      return {
        handled: true,
        text: webetuVaultReadyText(api, status.publicUserId),
      };
    }
    const invite = await accountLinkInvite(api, sender, "webetu", "/vault");
    if (!invite.setupUrl) throw new Error("missing setupUrl");
    return {
      handled: true,
      text: webetuAccountLinkStart(api, invite.setupUrl),
    };
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    api.logger?.warn?.(`public-service-gate: failed to create webetu account link (${String(err)})`);
    return {
      handled: true,
      text: "I couldn't start Webetu setup. Try again later.",
    };
  }
}

export async function handleWebetuSetupDone(api, sender) {
  if (!hasPendingWebetuVaultLink(api, sender)) {
    return {
      handled: true,
      text: "What did you finish setting up: Webetu, personalized news, or Job Scout?",
    };
  }

  try {
    const status = await accountLinkStatus(api, sender);
    if (!status.linked) {
      return {
        handled: true,
        text: await webetuOnboardingRedirectText(api, sender),
      };
    }

    const lookup = await workerLookupUser(api, sender);
    if (!lookup || lookup.status !== "ok" || lookup.registration_status !== "active") {
      return {
        handled: true,
        text: WEBETU_SETUP_NOT_CONFIRMED,
      };
    }

    await runWorkerSafe(api, ["check-credentials", "--phone", sender]);
    await runWorkerSafe(api, ["reserve", "--phone", sender, "--dry-run"]);
    saveServiceState(api, sender, "webetu", "webetu.awaiting_restaurant");
    return {
      handled: true,
      text: "Your Webetu details are saved. Which restaurant should I use by default?",
    };
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    logWorkerFailure(api, "webetu setup completion", err);
    return {
      handled: true,
      text: WEBETU_SETUP_NOT_CONFIRMED,
    };
  }
}

// Reservation-status branch of the member router. Extracted verbatim so the
// worker primitives stay encapsulated in this module.
export async function handleReservationStatus(api, sender) {
  try {
    const lookup = await workerLookupUser(api, sender);
    if (!lookup || lookup.status !== "ok" || lookup.registration_status !== "active") {
      try {
        return {
          handled: true,
          text: await webetuOnboardingRedirectText(api, sender),
        };
      } catch (err) {
        if (isQueueFullError(err)) return busyReply();
        logWorkerFailure(api, "reservation status onboarding redirect", err);
        return {
          handled: true,
          text: "Meal reservations aren't set up yet. Send \"start webetu\" to begin.",
        };
      }
    }
    const reservation = await runWorkerSafe(api, ["reserve", "--phone", sender, "--dry-run"]);
    return {
      handled: true,
      text: formatReservationStatusReply(reservation),
    };
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    logWorkerFailure(api, "reservation status", err);
    return {
      handled: true,
      text: "I couldn't check your reservation. Try again later.",
    };
  }
}

// Continuation handler for the webetu.awaiting_restaurant / restaurant_not_found
// states. Returns a reply object, or undefined to defer to the relevance gate.
export function handleWebetuRestaurantChoice(api, sender, text) {
  if (isWebetuWaitChoice(text)) {
    clearServiceState(api, sender);
    return {
      handled: true,
      text: WEBETU_RESTAURANT_WAIT_REPLY,
    };
  }
  if (isWebetuUseSupportedChoice(text)) {
    saveServiceState(api, sender, "webetu", "webetu.awaiting_restaurant");
    return {
      handled: true,
      text: WEBETU_RESTAURANT_USE_SUPPORTED_REPLY,
    };
  }
  if (shouldClarifyWebetuRestaurantChoice(text)) {
    return {
      handled: true,
      text: WEBETU_RESTAURANT_CLARIFY_REPLY,
    };
  }
  return undefined;
}
