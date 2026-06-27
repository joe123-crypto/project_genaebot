import { expandHome, getPluginConfig } from "../lib/config.js";
import { NEWS_HELPER_TIMEOUT_MS, busyReply, helperLimiter, isQueueFullError, runJsonCommand } from "../lib/runtime.js";
import { clearServiceState, saveServiceState } from "../lib/state.js";
import { accountLinkInvite, accountLinkRequiredText, accountLinkStatus } from "../lib/account-link.js";

const DEFAULT_NEWS_SUBSCRIPTIONS_SCRIPT = "~/.openclaw/workspace/public-news/news_subscriptions.py";
const DEFAULT_NEWS_TOPIC_PROMPT = `Which topics should I follow?

For example: AI, football, business, or scholarships.`;
const DEFAULT_NEWS_STOPPED = `Personalized news is off.`;

const NEWS_STOP_PATTERNS = [
  /\b(?:stop|unsubscribe|cancel|disable|turn\s+off)\b.*\b(?:personalized\s+)?news\b/i,
  /\b(?:personalized\s+)?news\b.*\b(?:stop|unsubscribe|cancel|disable|turn\s+off)\b/i,
];
const NEWS_REQUEST_PATTERNS = [
  /\b(?:personalized\s+)?news\b/i,
  /\b(?:register|subscribe|sign\s*me\s*up|start|setup|set\s*up)\b.*\bnews\b/i,
  /\bnews\b.*\b(?:register|subscribe|sign\s*me\s*up|start|setup|set\s*up|topics?|follow|about|on)\b/i,
];

export { DEFAULT_NEWS_STOPPED };

function getNewsSubscriptionsScript(api) {
  const pluginCfg = getPluginConfig(api);
  const configuredPath =
    typeof pluginCfg.newsSubscriptionsScript === "string" && pluginCfg.newsSubscriptionsScript.trim()
      ? pluginCfg.newsSubscriptionsScript.trim()
      : DEFAULT_NEWS_SUBSCRIPTIONS_SCRIPT;
  return expandHome(configuredPath);
}

export function runNewsSubscriptionScript(api, args) {
  const script = getNewsSubscriptionsScript(api);
  return helperLimiter(() => runJsonCommand("python3", [script, ...args], { timeoutMs: NEWS_HELPER_TIMEOUT_MS }));
}

export function isNewsStopRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 260) return false;
  return NEWS_STOP_PATTERNS.some((pattern) => pattern.test(compact));
}

export function isNewsRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 360) return false;
  return NEWS_REQUEST_PATTERNS.some((pattern) => pattern.test(compact));
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

function newsTopicsLabel(topics) {
  if (!Array.isArray(topics) || topics.length === 0) return "";
  return topics.slice(0, 5).join(", ");
}

export async function handleNewsRequest(api, sender, text) {
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
      text: "I couldn't start personalized news. Try again later.",
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
      text: "I couldn't save those topics. Try again later.",
    };
  }

  clearServiceState(api, sender);
  return {
    handled: true,
    text: `Personalized news is set for: ${newsTopicsLabel(topics)}. I'll send updates here.`,
  };
}

export async function handleNewsTopicsContinuation(api, sender, text) {
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
      text: "I couldn't start personalized news. Try again later.",
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
      text: "I couldn't save those topics. Try again later.",
    };
  }

  clearServiceState(api, sender);
  return {
    handled: true,
    text: `Personalized news is set for: ${newsTopicsLabel(topics)}. I'll send updates here.`,
  };
}
