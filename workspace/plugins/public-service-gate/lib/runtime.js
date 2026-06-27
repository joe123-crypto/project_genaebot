import { execFile } from "node:child_process";

export const DEFAULT_COMMAND_MAX_BUFFER = 1024 * 1024;
export const HELPER_TIMEOUT_MS = 15000;
export const NEWS_HELPER_TIMEOUT_MS = 10000;
export const WEBETU_TIMEOUT_MS = 30000;

const DEFAULT_BUSY_MESSAGE = `I'm busy right now. Try again shortly.`;

export class QueueFullError extends Error {
  constructor(name) {
    super(`${name} queue is full`);
    this.name = "QueueFullError";
    this.queueName = name;
  }
}

export function createLimiter({ name, maxConcurrent, maxQueue }) {
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

export const helperLimiter = createLimiter({ name: "helper", maxConcurrent: 4, maxQueue: 40 });
export const webetuLimiter = createLimiter({ name: "webetu", maxConcurrent: 2, maxQueue: 20 });

export function isQueueFullError(err) {
  return err?.name === "QueueFullError";
}

export function busyReply() {
  return { handled: true, text: DEFAULT_BUSY_MESSAGE };
}

export function runTextCommand(command, args, options = {}) {
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

export async function runJsonCommand(command, args, options = {}) {
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

export function textFromErrorField(value) {
  if (Buffer.isBuffer(value)) return value.toString("utf8");
  return String(value ?? "");
}

export function sanitizeLogText(value) {
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
