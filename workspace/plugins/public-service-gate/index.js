import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";

const DEFAULT_GREETING_TEMPLATE_PATH = "~/.openclaw/workspace/docs/public-greeting.txt";
const DEFAULT_NEWS_SUBSCRIPTIONS_SCRIPT = "~/.openclaw/workspace/public-news/news_subscriptions.py";
const DEFAULT_JOB_SCOUT_SCRIPT = "~/.openclaw/workspace/bin/job-scout";
const DEFAULT_ACCOUNT_LINK_SCRIPT = "~/.openclaw/workspace/bin/account-link";
const DEFAULT_INVITE_SCRIPT = "~/.openclaw/workspace/bin/whatsapp-invite";
const DEFAULT_WORKER_SAFE_SCRIPT = "/usr/local/bin/webetu-worker-safe";
const DEFAULT_WORKER_SAFE_USER = "webetu-worker";
const DEFAULT_WORKER_SUDO_COMMAND = "sudo";
const DEFAULT_PENDING_LINKS_PATH = "/home/joseph/.openclaw/runtime/account-link/pending-links.json";
const DEFAULT_SERVICE_STATE_PATH = "/home/joseph/.openclaw/runtime/public-service-gate/state.json";
const RELEVANCE_GATE_ID = "whatsapp-relevance-gate";
const DEFAULT_GREETING = `Hello there, welcome to OpenClaw Service Desk. We help with:

1. Webetu Progress meal reservation
2. Personalized news delivery to your inbox
3. Local event scouting around your location
4. Job, scholarship, and internship scouting/application

Please tell me which service you need help with.`;
const DEFAULT_WEBETU_ONBOARDING_START = `Open this link and sign in with your email:
{setupUrl}

After sign-in, the app will open your Credentials Vault. Save your Webetu username and password there. Do not send your Webetu username or password here.

Then come back here after saving your credentials. I will fetch your supported Webetu restaurants and confirm your default restaurant before saving it.

If the sign-in email does not arrive, check your Spam or Junk folder.`;
const DEFAULT_NEWS_TOPIC_PROMPT = `What topics should I follow for you?

Example: AI, football, business, scholarships, Zimbabwe news.`;
const DEFAULT_NEWS_STOPPED = `Personalized news has been stopped for this WhatsApp chat.`;
const DEFAULT_JOB_SCOUT_SETUP_FAILED = `I couldn't start Job Scout setup right now. Please try again later.`;
const DEFAULT_INVITE_FAILED = `I couldn't create an invite link right now. Please try again later.`;
const DEFAULT_INVITE_INVALID = `This invite link is invalid or has already been used. Ask your friend for a new one.`;
const DEFAULT_INVITE_REQUIRED = `This service is invite-only. Ask a friend who is already signed in and using Genaie Service Desk to send you their invite link. Open that link and send the pre-filled JOIN message here.`;
const DEFAULT_INVITE_PENDING = `Your WhatsApp number is activated.

Your account is not unlocked yet. Type "invite link", send the link to one person. When they activate with your link, your access will unlock.`;
const DEFAULT_INVITE_UNLOCKED = `Your invited person activated their WhatsApp number. Your access to Genaie Service Desk is now unlocked.`;
const DEFAULT_INVITE_AWAITING_ACTIVATION = `Got it — waiting for them to activate. Your access unlocks as soon as they do.`;
const DEFAULT_EVENT_SCOUT_SETUP_FAILED = `I couldn't start event scouting setup right now. Please try again later.`;
const DEFAULT_BUSY_MESSAGE = `I am handling a few requests right now. Please try again shortly.`;
const WEBETU_RESTAURANT_WAIT_REPLY = `Understood. Make manual reservations on Webetu until your restaurant shows up. When you see it, message me and I'll set it as your default and fill your next 3 days.`;
const WEBETU_RESTAURANT_USE_SUPPORTED_REPLY = `Send the exact restaurant name from the supported list you want me to use.`;
const WEBETU_RESTAURANT_CLARIFY_REPLY = `Do you want to use one of the supported restaurants now, or wait until your restaurant appears?`;
const WEBETU_SETUP_NOT_CONFIRMED = `I couldn't confirm your Webetu setup yet.

Open your Credentials Vault and make sure your Webetu username and password are saved there. Then message me again.`;
const DEFAULT_COMMAND_MAX_BUFFER = 1024 * 1024;
const HELPER_TIMEOUT_MS = 15000;
const NEWS_HELPER_TIMEOUT_MS = 10000;
const WEBETU_TIMEOUT_MS = 30000;
const SERVICE_STATE_TTL_MS = 30 * 60 * 1000;

class QueueFullError extends Error {
  constructor(name) {
    super(`${name} queue is full`);
    this.name = "QueueFullError";
    this.queueName = name;
  }
}

function createLimiter({ name, maxConcurrent, maxQueue }) {
  let active = 0;
  const queue = [];

  function drain() {
    while (active < maxConcurrent && queue.length > 0) {
      const item = queue.shift();
      active += 1;
      Promise.resolve()
        .then(item.task)
        .then(item.resolve, item.reject)
        .finally(() => {
          active -= 1;
          drain();
        });
    }
  }

  return function limit(task) {
    if (active >= maxConcurrent && queue.length >= maxQueue) {
      return Promise.reject(new QueueFullError(name));
    }
    return new Promise((resolve, reject) => {
      queue.push({ task, resolve, reject });
      drain();
    });
  };
}

const helperLimiter = createLimiter({ name: "helper", maxConcurrent: 4, maxQueue: 40 });
const webetuLimiter = createLimiter({ name: "webetu", maxConcurrent: 2, maxQueue: 20 });

const GREETING_PATTERNS = [
  /^(?:hi|hello|hey|hie|yo|salam|salaam|bonjour|good\s+(?:morning|afternoon|evening)|gm|morning|afternoon|evening)[\s!.?]*$/i,
  /^(?:hi|hello|hey|hie|salam|salaam|bonjour)\s+(?:there|openclaw|bot|assistant|author)[\s!.?]*$/i,
  /^(?:start|menu|help|services|service|options)[\s!.?]*$/i,
  /^(?:what\s+(?:can\s+you\s+do|services\s+do\s+you\s+(?:offer|provide))|what\s+do\s+you\s+(?:do|offer|provide))[\s!.?]*$/i,
];
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
const NEWS_STOP_PATTERNS = [
  /\b(?:stop|unsubscribe|cancel|disable|turn\s+off)\b.*\b(?:personalized\s+)?news\b/i,
  /\b(?:personalized\s+)?news\b.*\b(?:stop|unsubscribe|cancel|disable|turn\s+off)\b/i,
];
const NEWS_REQUEST_PATTERNS = [
  /\b(?:personalized\s+)?news\b/i,
  /\b(?:register|subscribe|sign\s*me\s*up|start|setup|set\s*up)\b.*\bnews\b/i,
  /\bnews\b.*\b(?:register|subscribe|sign\s*me\s*up|start|setup|set\s*up|topics?|follow|about|on)\b/i,
];
// An existing user (member or admin) asking for a link to invite someone else.
const INVITE_REQUEST_PATTERNS = [
  /\binvite\s+(?:link|code)\b/i,
  /\b(?:invite|refer)\s+(?:a\s+|my\s+|some\s+)?(?:friend|friends|someone|somebody|people|others?|buddy|buddies)\b/i,
  /\b(?:get|need|want|send|give|create|make|generate)\b[^.!?]*\binvite\b/i,
  /\b(?:how\s+(?:do|can)\s+i|can\s+i)\s+invite\b/i,
  /^invite(?:\s+me)?[\s!.?]*$/i,
];
// A pending user confirming they have shared their invite link, e.g. "Shared",
// "Sent it", "Done". Matched as a whole message so it doesn't fire on longer
// sentences that merely contain these words.
const INVITE_SHARED_KEYWORDS = [
  "shared",
  "shared it",
  "i shared",
  "i shared it",
  "sent",
  "sent it",
  "i sent",
  "i sent it",
  "forwarded",
  "i forwarded",
  "done",
];
// A new (not-yet-allowlisted) person sending the pre-filled wa.me join message,
// e.g. "JOIN-AB12CD34". Capture group 1 is the bare code.
const JOIN_CODE_PATTERN = /\bjoin[-\s]?([a-z0-9]{6,12})\b/i;
const JOB_SCOUT_REQUEST_PATTERNS = [
  /\bjob\s*(?:scout|scouting|application|applications?)\b/i,
  /\b(?:apply|applying)\b.*\bjobs?\b/i,
  /\b(?:find|look\s+for|search\s+for|scout)\b.*\b(?:jobs?|internships?|scholarships?)\b/i,
  /\b(?:jobs?|internships?|scholarships?)\b.*\b(?:find|search|scout|apply|application|applications?|register|subscribe|start|setup|set\s*up)\b/i,
];
const EVENT_SCOUT_REQUEST_PATTERNS = [
  /\bevent\s*(?:scout|scouting)\b/i,
  /\blocal\s+events?\b/i,
  /\b(?:find|look\s+for|search\s+for|scout|register|subscribe|sign\s*me\s*up|start|setup|set\s*up)\b.*\bevents?\b/i,
  /\bevents?\b.*\b(?:find|search|scout|register|subscribe|start|setup|set\s*up|calendar|remind(?:er|ers)?|near\s*me|in\s+my\s+area)\b/i,
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
const DEFAULT_SHORT_CONTINUATION_KEYWORDS = [
  "1",
  "2",
  "3",
  "4",
  "done",
  "finished",
  "complete",
  "completed",
  "saved",
  "ok",
  "okay",
  "yes",
  "no",
  "sure",
  "thanks",
  "thank you",
];
const DEFAULT_RELEVANCE_MAX_TEXT_LENGTH = 400;
const DEFAULT_RELEVANCE_LLM_MAX_TOKENS = 5;
const FAILSAFE_STOP_PRIORITY = 100;
const FAILSAFE_STOP_SUPPRESSION_TTL_MS = 30 * 60 * 1000;
const failsafeStopSuppressions = new Map();

function readOpenclawEnv() {
  try {
    const envPath = path.join(os.homedir(), ".openclaw", ".env");
    const text = fs.readFileSync(envPath, "utf8");
    const values = {};
    for (const rawLine of text.split("\n")) {
      let line = rawLine.trim();
      if (!line || line.startsWith("#")) continue;
      const eqIdx = line.indexOf("=");
      if (eqIdx === -1) continue;
      let key = line.slice(0, eqIdx).trim();
      if (key.startsWith("export ")) key = key.slice(7).trim();
      let value = line.slice(eqIdx + 1).trim();
      if (value.length >= 2 && value[0] === value[value.length - 1] && (value[0] === '"' || value[0] === "'")) {
        value = value.slice(1, -1);
      }
      if (key) values[key] = value;
    }
    return values;
  } catch {
    return {};
  }
}

function resolveAppPublicBaseUrl() {
  if (process.env.PUBLIC_BASE_URL) return process.env.PUBLIC_BASE_URL.replace(/\/+$/, "");
  if (process.env.APP_BASE_URL) return process.env.APP_BASE_URL.replace(/\/+$/, "");
  const env = readOpenclawEnv();
  if (env.PUBLIC_BASE_URL) return env.PUBLIC_BASE_URL.replace(/\/+$/, "");
  if (env.APP_BASE_URL) return env.APP_BASE_URL.replace(/\/+$/, "");
  return "http://localhost:3000";
}

function readEnvValue(name) {
  if (Object.prototype.hasOwnProperty.call(process.env, name)) return process.env[name];
  return readOpenclawEnv()[name];
}

function parseBoolean(value) {
  if (typeof value === "boolean") return value;
  if (typeof value !== "string") return undefined;
  const normalized = value.trim().toLowerCase();
  if (["1", "true", "yes", "y", "on"].includes(normalized)) return true;
  if (["0", "false", "no", "n", "off"].includes(normalized)) return false;
  return undefined;
}

function expandHome(input) {
  if (typeof input !== "string" || input.length === 0) return input;
  if (input === "~") return os.homedir();
  if (input.startsWith("~/")) return path.join(os.homedir(), input.slice(2));
  return input;
}

function normalizePhone(value) {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  if (!trimmed) return "";
  const plus = trimmed.startsWith("+") ? "+" : "";
  return plus + trimmed.replace(/[^\d]/g, "");
}

function getEventChannel(event, ctx = {}) {
  return String(event?.channel ?? ctx?.channelId ?? ctx?.channel ?? "").trim();
}

function isWhatsAppChannel(value) {
  const channel = String(value ?? "").trim().toLowerCase();
  return channel === "whatsapp" || channel.startsWith("whatsapp:");
}

function textCandidate(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value
      .map((part) => {
        if (typeof part === "string") return part;
        if (typeof part?.text === "string") return part.text;
        if (typeof part?.content === "string") return part.content;
        return "";
      })
      .join(" ");
  }
  return "";
}

function getEventText(event = {}) {
  const candidates = [
    event.content,
    event.body,
    event.text,
    event.message?.text,
    event.message?.body,
    event.message?.content,
    event.payload?.text,
    event.payload?.body,
    event.payload?.content,
  ];
  for (const candidate of candidates) {
    const text = textCandidate(candidate).trim();
    if (text) return text;
  }
  return "";
}

function getEventSender(event = {}, ctx = {}) {
  return normalizePhone(
    event.senderId ??
      event.sender_id ??
      event.from ??
      event.sender?.e164 ??
      event.sender?.id ??
      event.sender?.phone ??
      event.message?.from ??
      event.payload?.from ??
      ctx.senderId ??
      ctx.sender_id ??
      ctx.from ??
      ctx.sender?.e164 ??
      ctx.sender?.id ??
      "",
  );
}

function stopSuppressionKey(value) {
  const digits = String(value ?? "").replace(/[^\d]/g, "");
  return digits || "";
}

function markFailsafeStopSuppression(sender, now = Date.now()) {
  const key = stopSuppressionKey(sender);
  if (!key) return;
  failsafeStopSuppressions.set(key, now + FAILSAFE_STOP_SUPPRESSION_TTL_MS);
}

function clearFailsafeStopSuppression(sender) {
  const key = stopSuppressionKey(sender);
  if (!key) return;
  failsafeStopSuppressions.delete(key);
}

function hasFailsafeStopSuppression(value, now = Date.now()) {
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

function getOutboundRecipient(event = {}, ctx = {}) {
  return (
    event.to ??
    event.target ??
    event.recipient ??
    event.recipientId ??
    event.metadata?.to ??
    event.metadata?.recipient ??
    ctx.senderId ??
    ctx.sender_id ??
    ctx.from ??
    ctx.sender?.e164 ??
    ctx.sender?.id ??
    ""
  );
}

function getCurrentConfig(api) {
  return api.runtime?.config?.current?.() ?? api.config ?? {};
}

function getPluginConfig(api) {
  const live = getCurrentConfig(api).plugins?.entries?.["public-service-gate"]?.config;
  return live && typeof live === "object" ? live : api.pluginConfig ?? {};
}

function getRelevanceGateConfig(api) {
  const live = getCurrentConfig(api).plugins?.entries?.[RELEVANCE_GATE_ID]?.config;
  return live && typeof live === "object" ? live : {};
}

function normalizeText(text) {
  return String(text ?? "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

function extractLlmText(res) {
  if (!res) return "";
  if (typeof res === "string") return res;
  if (typeof res.text === "string") return res.text;
  const content = res.content ?? res.message?.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) =>
        typeof part === "string" ? part : typeof part?.text === "string" ? part.text : "",
      )
      .join(" ");
  }
  return "";
}

function parseRelevanceVerdict(llmText) {
  const upper = String(llmText ?? "").toUpperCase();
  if (upper.includes("IRRELEVANT")) return "irrelevant";
  if (upper.includes("RELEVANT")) return "relevant";
  return null;
}

function getPublicUsers(api) {
  const pluginCfg = getPluginConfig(api);
  const explicit = Array.isArray(pluginCfg.publicUsers) ? pluginCfg.publicUsers : [];
  return new Set(explicit.map(normalizePhone).filter(Boolean));
}

function isEnabled(api) {
  const pluginCfg = getPluginConfig(api);
  return pluginCfg.enabled !== false;
}

function isWhatsAppPublicUser(api, event, ctx) {
  if (!isWhatsAppChannel(getEventChannel(event, ctx))) return false;
  const sender = getEventSender(event, ctx);
  if (!sender) return false;
  return getPublicUsers(api).has(sender);
}

function isGreetingOrServiceMenuRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 120) return false;
  return GREETING_PATTERNS.some((pattern) => pattern.test(compact));
}

function isWebetuOnboardingStartRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 220) return false;
  if (/@/.test(compact)) return false;
  return WEBETU_ONBOARDING_PATTERNS.some((pattern) => pattern.test(compact));
}

function isWebetuGuideRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 220) return false;
  return WEBETU_GUIDE_PATTERNS.some((pattern) => pattern.test(compact));
}

function isWebetuSetupDoneRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 220) return false;
  return WEBETU_SETUP_DONE_PATTERNS.some((pattern) => pattern.test(compact));
}

function getServiceMenuSelection(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  const match = /^([1-4])[\s.)-]*$/.exec(compact);
  return match ? match[1] : "";
}

function isReservationStatusRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 220) return false;
  return RESERVATION_STATUS_PATTERNS.some((pattern) => pattern.test(compact));
}

function matchesSharedAck(text) {
  const compact = String(text ?? "")
    .trim()
    .replace(/\s+/g, " ")
    .toLowerCase()
    .replace(/[\s!.?]+$/, "");
  if (!compact) return false;
  return INVITE_SHARED_KEYWORDS.includes(compact);
}

function getShortContinuationKeywords(api) {
  const cfg = getRelevanceGateConfig(api);
  const configured = Array.isArray(cfg.shortContinuationKeywords)
    ? cfg.shortContinuationKeywords.map(normalizeText).filter(Boolean)
    : [];
  return configured.length > 0 ? configured : DEFAULT_SHORT_CONTINUATION_KEYWORDS;
}

function relevanceKeywordMatch(api, normalized) {
  if (getShortContinuationKeywords(api).includes(normalized)) return true;
  const cfg = getRelevanceGateConfig(api);
  const keywords = Array.isArray(cfg.serviceKeywords) ? cfg.serviceKeywords : [];
  for (const kw of keywords) {
    const k = normalizeText(kw);
    if (k && normalized.includes(k)) return true;
  }
  const examples = Array.isArray(cfg.serviceExamples) ? cfg.serviceExamples : [];
  for (const ex of examples) {
    const e = normalizeText(ex);
    if (!e) continue;
    if (normalized.includes(e) || e.includes(normalized)) return true;
  }
  return false;
}

function isKnownServiceRequestText(text) {
  return Boolean(
    getServiceMenuSelection(text) ||
      isGreetingOrServiceMenuRequest(text) ||
      isInviteRequest(text) ||
      isWebetuOnboardingStartRequest(text) ||
      isWebetuGuideRequest(text) ||
      isWebetuSetupDoneRequest(text) ||
      isReservationStatusRequest(text) ||
      isNewsStopRequest(text) ||
      isNewsRequest(text) ||
      isJobScoutRequest(text) ||
      isEventScoutRequest(text),
  );
}

async function isUnknownSenderRelevant(api, text) {
  if (isKnownServiceRequestText(text)) return true;

  const normalized = normalizeText(text);
  if (!normalized) return false;

  const cfg = getRelevanceGateConfig(api);
  if (cfg.enabled === false) return false;

  const maxLen = Number.isInteger(cfg.maxTextLength) ? cfg.maxTextLength : DEFAULT_RELEVANCE_MAX_TEXT_LENGTH;
  if (normalized.length > maxLen) return false;

  if (relevanceKeywordMatch(api, normalized)) return true;
  if (cfg.useLlmFallback === false) return false;

  const complete = api.runtime?.llm?.complete;
  if (typeof complete !== "function") return false;

  try {
    const summary =
      typeof cfg.serviceSummary === "string" && cfg.serviceSummary.trim()
        ? cfg.serviceSummary.trim()
        : "the services this assistant offers";
    const res = await complete({
      purpose: "public-service-gate.unknown_sender_relevance",
      maxTokens: Number.isInteger(cfg.llmMaxTokens) ? cfg.llmMaxTokens : DEFAULT_RELEVANCE_LLM_MAX_TOKENS,
      temperature: 0,
      messages: [
        {
          role: "user",
          content:
            `Services offered: ${summary}\n\n` +
            `Is the following user message a request related to these services? ` +
            `Answer with exactly one word: RELEVANT or IRRELEVANT.\n\n` +
            `Message: ${text}`,
        },
      ],
    });
    return parseRelevanceVerdict(extractLlmText(res)) === "relevant";
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: unknown sender classifier error (${String(err)})`);
    return false;
  }
}

function loadGreeting(api) {
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

function loadInviteRequiredText(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.inviteRequiredText === "string" && pluginCfg.inviteRequiredText.trim()
    ? pluginCfg.inviteRequiredText.trim()
    : DEFAULT_INVITE_REQUIRED;
}

function loadInvitePendingText(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.invitePendingText === "string" && pluginCfg.invitePendingText.trim()
    ? pluginCfg.invitePendingText.trim()
    : DEFAULT_INVITE_PENDING;
}

function loadInviteUnlockedText(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.inviteUnlockedText === "string" && pluginCfg.inviteUnlockedText.trim()
    ? pluginCfg.inviteUnlockedText.trim()
    : DEFAULT_INVITE_UNLOCKED;
}

function loadInviteAwaitingText(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.inviteAwaitingActivationText === "string" && pluginCfg.inviteAwaitingActivationText.trim()
    ? pluginCfg.inviteAwaitingActivationText.trim()
    : DEFAULT_INVITE_AWAITING_ACTIVATION;
}

function loadWebetuOnboardingStart(api) {
  const pluginCfg = getPluginConfig(api);
  return typeof pluginCfg.webetuOnboardingStartText === "string" && pluginCfg.webetuOnboardingStartText.trim()
    ? pluginCfg.webetuOnboardingStartText.trim()
    : DEFAULT_WEBETU_ONBOARDING_START;
}

function webetuAccountLinkStart(api, setupUrl) {
  return loadWebetuOnboardingStart(api).replaceAll("{setupUrl}", setupUrl);
}

function getNewsSubscriptionsScript(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.newsSubscriptionsScript === "string" && pluginCfg.newsSubscriptionsScript.trim()
      ? pluginCfg.newsSubscriptionsScript.trim()
      : DEFAULT_NEWS_SUBSCRIPTIONS_SCRIPT;
  return expandHome(configuredPath);
}

function getJobScoutScript(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.jobScoutScript === "string" && pluginCfg.jobScoutScript.trim()
      ? pluginCfg.jobScoutScript.trim()
      : DEFAULT_JOB_SCOUT_SCRIPT;
  return expandHome(configuredPath);
}

function getAccountLinkScript(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.accountLinkScript === "string" && pluginCfg.accountLinkScript.trim()
      ? pluginCfg.accountLinkScript.trim()
      : DEFAULT_ACCOUNT_LINK_SCRIPT;
  return expandHome(configuredPath);
}

function getPendingLinksPath(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.pendingLinksPath === "string" && pluginCfg.pendingLinksPath.trim()
      ? pluginCfg.pendingLinksPath.trim()
      : DEFAULT_PENDING_LINKS_PATH;
  return expandHome(configuredPath);
}

function getServiceStatePath(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.serviceStatePath === "string" && pluginCfg.serviceStatePath.trim()
      ? pluginCfg.serviceStatePath.trim()
      : DEFAULT_SERVICE_STATE_PATH;
  return expandHome(configuredPath);
}

function parseIsoTime(value) {
  const text = String(value ?? "").trim();
  if (!text) return null;
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? null : date;
}

function readPendingLinks(api) {
  try {
    const raw = fs.readFileSync(getPendingLinksPath(api), "utf8");
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed;
    if (Array.isArray(parsed?.links)) return parsed.links;
  } catch (err) {
    api.logger?.debug?.(`public-service-gate: pending link cache unavailable (${String(err)})`);
  }
  return [];
}

function hasPendingWebetuVaultLink(api, phone) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return false;
  const now = Date.now();
  return readPendingLinks(api).some((entry) => {
    if (!entry || typeof entry !== "object") return false;
    if (normalizePhone(entry.phone) !== normalizedPhone) return false;
    if (entry.service !== "account-link" || entry.purpose !== "webetu" || entry.nextPath !== "/vault") return false;
    if (!String(entry.setupUrl ?? "").trim()) return false;
    const expiresAt = parseIsoTime(entry.expiresAt);
    return Boolean(expiresAt && expiresAt.getTime() > now);
  });
}

function readServiceStates(api) {
  try {
    const raw = fs.readFileSync(getServiceStatePath(api), "utf8");
    const parsed = JSON.parse(raw);
    const states = parsed?.states && typeof parsed.states === "object" ? parsed.states : parsed;
    return states && typeof states === "object" && !Array.isArray(states) ? states : {};
  } catch (err) {
    api.logger?.debug?.(`public-service-gate: service state cache unavailable (${String(err)})`);
    return {};
  }
}

function writeServiceStates(api, states) {
  const statePath = getServiceStatePath(api);
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  const tmpPath = path.join(path.dirname(statePath), `.${path.basename(statePath)}.${process.pid}.tmp`);
  fs.writeFileSync(tmpPath, JSON.stringify({ version: 1, states }, null, 2) + "\n", { encoding: "utf8", mode: 0o600 });
  fs.renameSync(tmpPath, statePath);
}

function pruneServiceStates(states, now = Date.now()) {
  const next = {};
  let changed = false;
  for (const [phone, state] of Object.entries(states || {})) {
    if (!state || typeof state !== "object") {
      changed = true;
      continue;
    }
    const expiresAt = parseIsoTime(state.expiresAt);
    if (!expiresAt || expiresAt.getTime() <= now) {
      changed = true;
      continue;
    }
    next[phone] = state;
  }
  return { states: next, changed };
}

function loadServiceState(api, phone) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return null;
  const loaded = readServiceStates(api);
  const pruned = pruneServiceStates(loaded);
  if (pruned.changed) {
    try {
      writeServiceStates(api, pruned.states);
    } catch (err) {
      api.logger?.warn?.(`public-service-gate: failed to prune service state (${String(err)})`);
    }
  }
  const state = pruned.states[normalizedPhone];
  return state && typeof state === "object" ? state : null;
}

function saveServiceState(api, phone, service, step, data = {}) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return;
  const loaded = readServiceStates(api);
  const pruned = pruneServiceStates(loaded).states;
  pruned[normalizedPhone] = {
    phone: normalizedPhone,
    service,
    step,
    data: data && typeof data === "object" ? data : {},
    updatedAt: new Date().toISOString(),
    expiresAt: new Date(Date.now() + SERVICE_STATE_TTL_MS).toISOString(),
  };
  writeServiceStates(api, pruned);
}

function clearServiceState(api, phone) {
  const normalizedPhone = normalizePhone(phone);
  if (!normalizedPhone) return;
  const loaded = readServiceStates(api);
  const pruned = pruneServiceStates(loaded).states;
  if (!Object.prototype.hasOwnProperty.call(pruned, normalizedPhone)) return;
  delete pruned[normalizedPhone];
  writeServiceStates(api, pruned);
}

function getInviteScript(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.inviteScript === "string" && pluginCfg.inviteScript.trim()
      ? pluginCfg.inviteScript.trim()
      : DEFAULT_INVITE_SCRIPT;
  return expandHome(configuredPath);
}

function runInviteScript(api, args) {
  const script = getInviteScript(api);
  return helperLimiter(() => runJsonCommand(script, args, { timeoutMs: HELPER_TIMEOUT_MS }));
}

async function inviteMembershipStatus(api, phone) {
  try {
    return await runInviteScript(api, ["status", "--phone", phone]);
  } catch (err) {
    if (isQueueFullError(err)) throw err;
    api.logger?.warn?.(`public-service-gate: failed to check invite membership status (${String(err)})`);
    return { status: "unknown", pending: false, unlocked: false };
  }
}

function isPendingInviteMembership(status) {
  return Boolean(status?.pending || status?.status === "pending");
}

function isUnlockedInviteMembership(status) {
  return Boolean(status?.unlocked || status?.status === "unlocked");
}

function getAdminUsers(api) {
  const pluginCfg = getPluginConfig(api);
  const explicit = Array.isArray(pluginCfg.whatsappAdminUsers) ? pluginCfg.whatsappAdminUsers : [];
  return new Set(explicit.map(normalizePhone).filter(Boolean));
}

function isAdminModeEnabled(api) {
  const envValue = parseBoolean(readEnvValue("WHATSAPP_ADMIN_ENABLED"));
  if (envValue !== undefined) return envValue;
  const pluginCfg = getPluginConfig(api);
  return pluginCfg.adminModeEnabled !== false;
}

function isWhatsAppAdmin(api, sender) {
  if (!sender) return false;
  if (!isAdminModeEnabled(api)) return false;
  return getAdminUsers(api).has(sender);
}

// Digits-only bot number used to build wa.me links; empty string if unconfigured.
function getBotWhatsappNumber(api) {
  const pluginCfg = getPluginConfig(api);
  const raw = typeof pluginCfg.botWhatsappNumber === "string" ? pluginCfg.botWhatsappNumber : "";
  return raw.replace(/[^\d]/g, "");
}

// Peer invites + open DMs are opt-in. When false, the gateway runs the classic
// allowlist and this plugin keeps its original member-only behavior.
function invitesEnabled(api) {
  const pluginCfg = getPluginConfig(api);
  return pluginCfg.invitesEnabled === true;
}

function isInviteRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 200) return false;
  return INVITE_REQUEST_PATTERNS.some((pattern) => pattern.test(compact));
}

function parseJoinCode(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 120) return "";
  const match = JOIN_CODE_PATTERN.exec(compact);
  return match ? match[1].toUpperCase() : "";
}

// Admin-only in-chat command to grandfather a pre-invite user, e.g.
// "grandfather +263788390903". Returns the normalized phone or "".
function parseGrandfatherCommand(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 60) return "";
  const match = /^grandfather\s+(\+?[\d][\d\s-]{6,})$/i.exec(compact);
  return match ? normalizePhone(match[1]) : "";
}

function isStateResetRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  return /^(?:menu|main\s+menu|services|service|options|help|start\s+over|restart|cancel)[\s!.?]*$/i.test(compact);
}

function isFailsafeStopCommand(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  return /^stop[!.?]*$/i.test(compact);
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

function inviteLinkText(api, code, options = {}) {
  const botDigits = getBotWhatsappNumber(api);
  const joinText = `JOIN-${code}`;
  if (!botDigits) {
    // Bot number not configured: hand over the code with instructions instead of a link.
    if (options.pending) {
      return `Send this invite to one person by asking them to message me:\n\n${joinText}\n\nWhen they activate with it, your service access will unlock. This invite works once.`;
    }
    return `Invite a friend by asking them to message me:\n\n${joinText}\n\nThey will need to invite one person before using the service. This invite works once.`;
  }
  const link = `https://wa.me/${botDigits}?text=${encodeURIComponent(joinText)}`;
  if (options.pending) {
    return `Here's your invite link. Send it to one person. When they tap it and activate their WhatsApp number, your service access will unlock:\n\n${link}\n\nThis invite works once.`;
  }
  return `Here's your invite link — share it with one friend. When they tap it and activate their WhatsApp number, they will need to invite one person before using the service:\n\n${link}\n\nThis invite works once.`;
}

// Best-effort proactive notification to the admin when someone joins via invite.
async function notifyAdminOfJoin(api, inviter, newMember) {
  try {
    const admin = [...getAdminUsers(api)][0];
    if (!admin) return;
    const msg =
      `New member joined via invite.\n` +
      `Invited by: ${inviter || "unknown"}\n` +
      `New member: ${newMember}`;
    await helperLimiter(() =>
      runTextCommand("openclaw", ["message", "send", "--channel", "whatsapp", "--target", admin, "--message", msg], {
        timeoutMs: HELPER_TIMEOUT_MS,
      }),
    );
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to notify admin of join (${String(err)})`);
  }
}

async function notifyInviterUnlocked(api, inviter, newMember) {
  try {
    const target = normalizePhone(inviter || "");
    if (!target) return;
    const msg = `${loadInviteUnlockedText(api)}\n\nActivated by: ${newMember}`;
    await helperLimiter(() =>
      runTextCommand("openclaw", ["message", "send", "--channel", "whatsapp", "--target", target, "--message", msg], {
        timeoutMs: HELPER_TIMEOUT_MS,
      }),
    );
  } catch (err) {
    api.logger?.warn?.(`public-service-gate: failed to notify inviter unlock (${String(err)})`);
  }
}

function getAgentGenaiePublicBaseUrl(api) {
  const pluginCfg = getPluginConfig(api);
  const configured =
    typeof pluginCfg.agentGenaiePublicBaseUrl === "string" && pluginCfg.agentGenaiePublicBaseUrl.trim()
      ? pluginCfg.agentGenaiePublicBaseUrl.trim()
      : resolveAppPublicBaseUrl();
  return configured.replace(/\/+$/, "");
}

function isNewsStopRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 260) return false;
  return NEWS_STOP_PATTERNS.some((pattern) => pattern.test(compact));
}

function isNewsRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 360) return false;
  return NEWS_REQUEST_PATTERNS.some((pattern) => pattern.test(compact));
}

function isJobScoutRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 420) return false;
  return JOB_SCOUT_REQUEST_PATTERNS.some((pattern) => pattern.test(compact));
}

function isEventScoutRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 420) return false;
  return EVENT_SCOUT_REQUEST_PATTERNS.some((pattern) => pattern.test(compact));
}

function splitTopics(value) {
  return String(value ?? "")
    .replace(/\s+(?:and|&)\s+/gi, ",")
    .split(/[,;\n]+/)
    .map((topic) => topic.replace(/\s+/g, " ").trim().replace(/^[.:\-\s]+|[.:\-\s]+$/g, ""))
    .filter(Boolean)
    .filter((topic) => !/^(?:please|thanks?|daily|weekly|every\s+day)$/i.test(topic))
    .slice(0, 12);
}

function extractNewsTopics(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact) return [];

  const explicit =
    compact.match(/\bnews\b(?:\s+updates?)?\s+(?:about|on|for|covering)\s+(.+)$/i) ??
    compact.match(/\b(?:topics?|follow)\b(?:\s+(?:are|is|for\s+news))?\s*[:\-]?\s+(.+)$/i);
  if (explicit?.[1]) return splitTopics(explicit[1]);

  const stripped = compact
    .replace(
      /^(?:please\s+)?(?:i\s+(?:want|need|would\s+like)\s+)?(?:register|subscribe|sign\s*me\s*up|start|setup|set\s*up|change|update)?\s*(?:me\s+)?(?:for\s+)?(?:personalized\s+)?news(?:\s+(?:to|for|about|on|with|topics?)\s*)?/i,
      "",
    )
    .trim();

  if (!stripped || stripped === compact || stripped.length < 2) return [];
  return splitTopics(stripped);
}

function runNewsSubscriptionScript(api, args) {
  const script = getNewsSubscriptionsScript(api);
  return helperLimiter(() => runJsonCommand("python3", [script, ...args], { timeoutMs: NEWS_HELPER_TIMEOUT_MS }));
}

function runJobScoutScript(api, args) {
  const script = getJobScoutScript(api);
  return helperLimiter(() => runJsonCommand(script, args, { timeoutMs: HELPER_TIMEOUT_MS }));
}

function runAccountLinkScript(api, args) {
  const script = getAccountLinkScript(api);
  return helperLimiter(() => runJsonCommand(script, args, { timeoutMs: HELPER_TIMEOUT_MS }));
}

function textFromErrorField(value) {
  if (Buffer.isBuffer(value)) return value.toString("utf8");
  return String(value ?? "");
}

function sanitizeLogText(value) {
  let text = textFromErrorField(value).replace(/\s+/g, " ").trim();
  if (!text) return "";
  text = text
    .replace(/(Authorization:\s*Bearer\s+)[^\s"'<>]+/gi, "$1[redacted]")
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[redacted]")
    .replace(/\b(AGENT_GENAI_INTERNAL_API_KEY|api[_-]?key|token|secret|password)\b\s*[:=]\s*["']?[^"',}\s]+/gi, "$1=[redacted]")
    .replace(/https?:\/\/[^\s"'<>?]+\?[^\s"'<>]+/gi, (match) => `${match.split("?")[0]}?[redacted]`)
    .replace(/\+\d{7,15}\b/g, "[phone]")
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[email]");
  return text.length > 1200 ? `${text.slice(0, 1200)}...` : text;
}

function isQueueFullError(err) {
  return err?.name === "QueueFullError";
}

function busyReply() {
  return { handled: true, text: DEFAULT_BUSY_MESSAGE };
}

function runTextCommand(command, args, options = {}) {
  const timeout = options.timeoutMs ?? HELPER_TIMEOUT_MS;
  const maxBuffer = options.maxBuffer ?? DEFAULT_COMMAND_MAX_BUFFER;
  return new Promise((resolve, reject) => {
    execFile(command, args, { encoding: "utf8", timeout, maxBuffer }, (err, stdout, stderr) => {
      if (err) {
        err.stdout = stdout;
        err.stderr = stderr;
        reject(err);
        return;
      }
      resolve(stdout);
    });
  });
}

async function runJsonCommand(command, args, options = {}) {
  const stdout = await runTextCommand(command, args, options);
  try {
    return JSON.parse(stdout);
  } catch (err) {
    err.stdout = stdout;
    err.stderr = "";
    err.message = `Command returned invalid JSON: ${err.message}`;
    throw err;
  }
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

function accountLinkStatus(api, phone) {
  return runAccountLinkScript(api, ["status", "--phone", phone]);
}

function accountLinkInvite(api, phone, purpose, nextPath) {
  return runAccountLinkScript(api, [
    "create-invite",
    "--phone",
    phone,
    "--purpose",
    purpose,
    "--next-path",
    nextPath,
  ]);
}

function scopedAppUrl(api, publicUserId, suffix = "") {
  const base = getAgentGenaiePublicBaseUrl(api);
  if (!publicUserId) return `${base}/login?next=${encodeURIComponent(suffix || "/")}`;
  return `${base}/${publicUserId}${suffix}`;
}

function newsTopicsLabel(topics) {
  if (!Array.isArray(topics) || topics.length === 0) return "";
  return topics.slice(0, 5).join(", ");
}

function jobScoutStartText(setupUrl) {
  return `Job Scout is active as a pilot.

Open this setup link and sign in with your email:
${setupUrl}

After sign-in, the app will open Gmail connection automatically.

Then choose one setup option here:
1. Answer a few short questions.
2. Upload your CV and allow me to pull the details from it.

I will ask only one or two questions at a time.`;
}

function eventScoutStartText(setupUrl) {
  return `Event scouting is active.

Open this setup link and sign in with your email:
${setupUrl}

After sign-in, connect Google and allow calendar access so I can add events to your calendar.

Then tell me what kinds of events you want (for example tech, business, or all), and I'll add matching upcoming events to your Google Calendar with reminders before each one.`;
}

function accountLinkRequiredText(setupUrl, serviceName) {
  return `${serviceName} needs to link this WhatsApp chat to your app login first.

Open this link and sign in with your email:
${setupUrl}

If the sign-in email does not arrive, check your Spam or Junk folder.`;
}

function webetuVaultReadyText(api, publicUserId) {
  return `Your WhatsApp chat is already linked.

Open your Credentials Vault here:
${scopedAppUrl(api, publicUserId, "/vault")}

Save your Webetu username and password there. Do not send your Webetu username or password here.

Then come back here after saving your credentials. I will fetch your supported Webetu restaurants and confirm your default restaurant before saving it.`;
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
    return `Your next meal reservation is set for ${when} at ${restaurant}. All 3 meal types will be booked automatically.`;
  }
  return `Your next meal reservation is set for ${when} at ${restaurant}. The remaining meals will be booked automatically.`;
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

async function handleWebetuOnboardingStart(api, sender) {
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
      text: "I couldn't start Webetu setup right now. Please try again later.",
    };
  }
}

async function handleWebetuSetupDone(api, sender) {
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
      text:
        "Your Webetu credentials are saved and the first check passed.\n\n" +
        "Which restaurant should I use by default for your Webetu meal reservations?",
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

async function handleNewsRequest(api, sender, text) {
  try {
    const status = await accountLinkStatus(api, sender);
    if (!status.linked) {
      const invite = await accountLinkInvite(api, sender, "news", "/");
      if (!invite.setupUrl) throw new Error("missing setupUrl");
      return {
        handled: true,
        text: accountLinkRequiredText(invite.setupUrl, "Personalized news"),
      };
    }
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    api.logger?.warn?.(`public-service-gate: failed to check news account link (${String(err)})`);
    return {
      handled: true,
      text: "I couldn't start personalized news setup right now. Please try again later.",
    };
  }

  const topics = extractNewsTopics(text);
  if (topics.length === 0) {
    saveServiceState(api, sender, "news", "news.awaiting_topics");
    return {
      handled: true,
      text: DEFAULT_NEWS_TOPIC_PROMPT,
    };
  }

  try {
    await runNewsSubscriptionScript(api, ["register", "--phone", sender, ...topics]);
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    api.logger?.warn?.(`public-service-gate: failed to register news (${String(err)})`);
    return {
      handled: true,
      text: "I couldn't save your news topics right now. Please try again later.",
    };
  }

  clearServiceState(api, sender);
  return {
    handled: true,
    text: `You're registered for personalized news: ${newsTopicsLabel(topics)}.\n\nI'll send updates to this WhatsApp chat.`,
  };
}

async function handleNewsTopicsContinuation(api, sender, text) {
  try {
    const status = await accountLinkStatus(api, sender);
    if (!status.linked) {
      clearServiceState(api, sender);
      const invite = await accountLinkInvite(api, sender, "news", "/");
      if (!invite.setupUrl) throw new Error("missing setupUrl");
      return {
        handled: true,
        text: accountLinkRequiredText(invite.setupUrl, "Personalized news"),
      };
    }
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    api.logger?.warn?.(`public-service-gate: failed to check news account link (${String(err)})`);
    return {
      handled: true,
      text: "I couldn't start personalized news setup right now. Please try again later.",
    };
  }

  const topics = splitTopics(text);
  if (topics.length === 0) {
    return {
      handled: true,
      text: DEFAULT_NEWS_TOPIC_PROMPT,
    };
  }

  try {
    await runNewsSubscriptionScript(api, ["register", "--phone", sender, ...topics]);
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    api.logger?.warn?.(`public-service-gate: failed to register news (${String(err)})`);
    return {
      handled: true,
      text: "I couldn't save your news topics right now. Please try again later.",
    };
  }

  clearServiceState(api, sender);
  return {
    handled: true,
    text: `You're registered for personalized news: ${newsTopicsLabel(topics)}.\n\nI'll send updates to this WhatsApp chat.`,
  };
}

async function handleJobScoutRequest(api, sender) {
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

async function handleEventScoutRequest(api, sender) {
  try {
    const status = await accountLinkStatus(api, sender);
    if (!status.linked) {
      const invite = await accountLinkInvite(api, sender, "events", "/connect-gmail");
      if (!invite.setupUrl) throw new Error("missing setupUrl");
      return {
        handled: true,
        text: accountLinkRequiredText(invite.setupUrl, "Event scouting"),
      };
    }
    const setupUrl = scopedAppUrl(api, status.publicUserId, "/connect-gmail");
    return {
      handled: true,
      text: eventScoutStartText(setupUrl),
    };
  } catch (err) {
    if (isQueueFullError(err)) return busyReply();
    api.logger?.warn?.(`public-service-gate: failed to start event scouting (${String(err)})`);
    return {
      handled: true,
      text: DEFAULT_EVENT_SCOUT_SETUP_FAILED,
    };
  }
}

async function handleActiveServiceState(api, sender, text, state) {
  if (!state || typeof state !== "object") return undefined;

  if (state.step === "webetu.awaiting_restaurant" || state.step === "webetu.restaurant_not_found") {
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

  if (state.step === "news.awaiting_topics") {
    return await handleNewsTopicsContinuation(api, sender, text);
  }

  return undefined;
}

export default {
  id: "public-service-gate",
  name: "Public Service Gate",
  description: "Handles deterministic public WhatsApp greetings before agent dispatch.",
  version: "0.1.0",
  register(api) {
    api.on(
      "before_dispatch",
      async (event, ctx) => {
        if (!isEnabled(api)) return;
        const channel = getEventChannel(event, ctx);
        if (!isWhatsAppChannel(channel)) return;
        const text = getEventText(event);
        const sender = getEventSender(event, ctx);
        if (!isFailsafeStopCommand(text)) {
          if (sender && text) clearFailsafeStopSuppression(sender);
          return;
        }

        if (sender) {
          markFailsafeStopSuppression(sender);
          try {
            clearServiceState(api, sender);
          } catch (err) {
            api.logger?.warn?.(`public-service-gate: failed to clear state for failsafe stop (${String(err)})`);
          }
        } else {
          api.logger?.warn?.("public-service-gate: WhatsApp failsafe stop missing sender; handled fail-closed");
        }
        api.logger?.info?.("public-service-gate: failsafe stop handled before agent dispatch");
        return { handled: true };
      },
      { priority: FAILSAFE_STOP_PRIORITY },
    );

    api.on(
      "message_sending",
      async (event, ctx) => {
        if (!isEnabled(api)) return;
        const channel = getEventChannel(event, ctx);
        if (!isWhatsAppChannel(channel)) return;
        const recipient = getOutboundRecipient(event, ctx);
        if (!hasFailsafeStopSuppression(recipient)) return;

        api.logger?.info?.("public-service-gate: suppressed WhatsApp outbound after failsafe stop");
        return {
          cancel: true,
          cancelReason: "failsafe_stop",
          metadata: { suppressedBy: "public-service-gate", reason: "failsafe_stop" },
        };
      },
      { priority: FAILSAFE_STOP_PRIORITY },
    );

    api.on("before_dispatch", async (event, ctx) => {
      if (!isEnabled(api)) return;
      const channel = getEventChannel(event, ctx);
      if (!isWhatsAppChannel(channel)) return;
      const text = getEventText(event);
      const sender = getEventSender(event, ctx);
      if (!sender) {
        api.logger?.warn?.("public-service-gate: WhatsApp before_dispatch missing sender; handled fail-closed");
        return { handled: true };
      }

      const admin = isWhatsAppAdmin(api, sender);
      const openInvites = invitesEnabled(api);

      // Admin escape hatch: unlock a pre-invite user from chat. Claimed here so
      // the command never reaches the agent or the relevance gate.
      if (admin && openInvites) {
        const grandfatherTarget = parseGrandfatherCommand(text);
        if (grandfatherTarget) {
          try {
            const res = await runInviteScript(api, ["grandfather", "--phone", grandfatherTarget]);
            api.logger?.info?.("public-service-gate: admin grandfather handled deterministically");
            return {
              handled: true,
              text: `Grandfathered ${grandfatherTarget}: ${res.changed ? "unlocked" : "already a member"}.`,
            };
          } catch (err) {
            if (isQueueFullError(err)) return busyReply();
            api.logger?.warn?.(`public-service-gate: failed to grandfather (${String(err)})`);
            return { handled: true, text: DEFAULT_INVITE_FAILED };
          }
        }
      }

      const configuredMember = !openInvites && isWhatsAppPublicUser(api, event, ctx);
      let inviteMembership;
      async function getInviteMembership() {
        if (inviteMembership !== undefined) return inviteMembership;
        inviteMembership = openInvites && !admin
          ? await inviteMembershipStatus(api, sender)
          : { status: "unknown", pending: false, unlocked: false };
        return inviteMembership;
      }
      const currentInviteMembership = openInvites && !admin
        ? await getInviteMembership()
        : { status: "unknown", pending: false, unlocked: false };
      const pendingInviteMember = isPendingInviteMembership(currentInviteMembership);
      const unlockedInviteMember = isUnlockedInviteMembership(currentInviteMembership);
      const member = !pendingInviteMember && (configuredMember || unlockedInviteMember);

      // Invite issuance for existing users and pending activated users. Claimed
      // here, ahead of the relevance gate, so an "invite link" request is never
      // blocked as off-topic.
      if (openInvites && (admin || member || pendingInviteMember) && isInviteRequest(text)) {
        try {
          const invite = await runInviteScript(api, ["create", "--inviter", sender]);
          if (!invite.code) throw new Error("missing code");
          api.logger?.info?.("public-service-gate: invite link handled deterministically");
          return { handled: true, text: inviteLinkText(api, invite.code, { pending: pendingInviteMember }) };
        } catch (err) {
          if (isQueueFullError(err)) return busyReply();
          api.logger?.warn?.(`public-service-gate: failed to create invite (${String(err)})`);
          return { handled: true, text: DEFAULT_INVITE_FAILED };
        }
      }

      if (pendingInviteMember) {
        if (matchesSharedAck(text)) {
          return { handled: true, text: loadInviteAwaitingText(api) };
        }
        return { handled: true, text: loadInvitePendingText(api) };
      }

      // Membership gate. Non-members can redeem JOIN codes; service-like messages
      // get an invite-required reply; unrelated traffic stays silent. Admins fall
      // through to the full agent.
      if (!member) {
        if (admin) return;
        if (!openInvites) return; // classic allowlist mode: leave to core access control
        const code = parseJoinCode(text);
        if (code) {
          try {
            const res = await runInviteScript(api, ["redeem", "--code", code, "--phone", sender]);
            if (res.ok) {
              const inviter = normalizePhone(res.inviter || "");
              const inviterWasAdmin = isWhatsAppAdmin(api, inviter);
              await notifyAdminOfJoin(api, inviter, sender);
              if (inviter && res.inviterUnlocked && !inviterWasAdmin) {
                await notifyInviterUnlocked(api, inviter, sender);
              }
              api.logger?.info?.("public-service-gate: invite join handled deterministically");
              return { handled: true, text: loadInvitePendingText(api) };
            }
            return { handled: true, text: DEFAULT_INVITE_INVALID };
          } catch (err) {
            if (isQueueFullError(err)) return busyReply();
            api.logger?.warn?.(`public-service-gate: failed to redeem invite (${String(err)})`);
            return { handled: true, text: DEFAULT_INVITE_INVALID };
          }
        }
        if (await isUnknownSenderRelevant(api, text)) {
          return { handled: true, text: loadInviteRequiredText(api) };
        }
        return { handled: true }; // silent drop unrelated traffic; claim it, no reply, no agent
      }

      // From here on, sender is a member: existing deterministic service handling.
      const activeState = loadServiceState(api, sender);
      if (activeState) {
        if (isStateResetRequest(text)) {
          clearServiceState(api, sender);
          return {
            handled: true,
            text: loadGreeting(api),
          };
        }
        const stateReply = await handleActiveServiceState(api, sender, text, activeState);
        if (stateReply) return stateReply;
        return;
      }

      const menuSelection = getServiceMenuSelection(text);

      if (menuSelection === "1" || isWebetuOnboardingStartRequest(text) || isWebetuGuideRequest(text)) {
        return await handleWebetuOnboardingStart(api, sender);
      }

      if (isWebetuSetupDoneRequest(text)) {
        return await handleWebetuSetupDone(api, sender);
      }

      if (isReservationStatusRequest(text)) {
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
                text: "You're not set up for meal reservations yet. Say \"start webetu\" and I'll help you set it up.",
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
            text: "I couldn't check your reservation right now. Please try again later.",
          };
        }
      }

      if (menuSelection === "2") {
        return await handleNewsRequest(api, sender, "news");
      }

      if (menuSelection === "3") {
        return await handleEventScoutRequest(api, sender);
      }

      if (menuSelection === "4") {
        return await handleJobScoutRequest(api, sender);
      }

      if (isNewsStopRequest(text)) {
        try {
          await runNewsSubscriptionScript(api, ["disable", "--phone", sender]);
          clearServiceState(api, sender);
        } catch (err) {
          if (isQueueFullError(err)) return busyReply();
          api.logger?.warn?.(`public-service-gate: failed to disable news (${String(err)})`);
          return {
            handled: true,
            text: "I couldn't stop personalized news right now. Please try again later.",
          };
        }
        return {
          handled: true,
          text: DEFAULT_NEWS_STOPPED,
        };
      }

      if (isNewsRequest(text)) {
        return await handleNewsRequest(api, sender, text);
      }

      if (isJobScoutRequest(text)) {
        return await handleJobScoutRequest(api, sender);
      }

      if (isEventScoutRequest(text)) {
        return await handleEventScoutRequest(api, sender);
      }

      if (isGreetingOrServiceMenuRequest(text)) {
        return {
          handled: true,
          text: loadGreeting(api),
        };
      }

      // No deterministic handler matched. Do NOT claim the message here — fall
      // through to the whatsapp-relevance-gate (priority -10), which is the
      // semantics allow/block gateway: on-topic text is passed to the agent,
      // off-topic text is rejected there with the same canned message.
      api.logger?.info?.(
        `public-service-gate: member WhatsApp text deferred to relevance gate sender=${sender} textLen=${text.length}`,
      );
      return;
    });
  },
};

export { QueueFullError, createLimiter, inviteLinkText, isQueueFullError, runJsonCommand };
