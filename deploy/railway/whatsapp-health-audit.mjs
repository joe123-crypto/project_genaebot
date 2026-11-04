// Lifecycle-only audit. No messages, recipients, credentials, or reconnect logic.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const EVENTS = new Set(['connect_attempt', 'connected', 'setup_failure', 'disconnected', 'watchdog', 'stopped']);
const REASONS = new Set(['transport-inactive', 'app-silent', 'logged-out', 'conflict', 'reconnecting',
  'connected', 'stopped', 'unknown', 'authentication', 'transport', 'shutdown']);
const RETENTION_MS = 14 * 86400_000;
const MAX_DAY_BYTES = 1024 * 1024;
const identifier = (value) => typeof value === 'string' && /^[a-zA-Z0-9_-]{1,128}$/.test(value) ? value : null;

export function createWhatsAppHealthAudit({ accountId, runId,
  root = process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw'),
  environment = process.env, now = () => new Date(), warn = console.warn,
  maxDayBytes = MAX_DAY_BYTES } = {}) {
  let prunedDay, warnedDay;
  return (input) => {
    if (environment.GENAIE_WHATSAPP_HEALTH_AUDIT === '0' || !EVENTS.has(input?.event)) return;
    try {
      const date = now(), day = date.toISOString().slice(0, 10);
      const directory = path.join(root, 'runtime', 'whatsapp-health');
      fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
      if (prunedDay !== day) {
        for (const name of fs.readdirSync(directory)) {
          const match = /^events-(\d{4}-\d{2}-\d{2})\.jsonl$/.exec(name);
          if (match && Date.parse(`${day}T00:00:00Z`) - Date.parse(`${match[1]}T00:00:00Z`) >= RETENTION_MS) {
            fs.unlinkSync(path.join(directory, name));
          }
        }
        prunedDay = day;
      }
      const statusCode = Number.isInteger(input.statusCode) ? input.statusCode : null;
      const reason = REASONS.has(input.reason) ? input.reason
        : statusCode === 401 ? 'authentication' : statusCode === 440 ? 'conflict'
          : statusCode !== null ? 'transport' : 'unknown';
      const record = { timestamp: date.toISOString(), accountId: identifier(accountId),
        deploymentId: identifier(environment.RAILWAY_DEPLOYMENT_ID), runId: identifier(runId),
        connectionId: identifier(input.connectionId), event: input.event, reason, statusCode,
        planned: input.planned === true };
      const file = path.join(directory, `events-${day}.jsonl`);
      const line = JSON.stringify(record) + '\n';
      const fd = fs.openSync(file, 'a', 0o600);
      try {
        if (fs.fstatSync(fd).size + Buffer.byteLength(line) > maxDayBytes) {
          if (warnedDay !== day) { warnedDay = day; warn('[whatsapp-health] daily audit limit reached'); }
          return;
        }
        fs.writeSync(fd, line);
      } finally { fs.closeSync(fd); }
    } catch {
      // Audit failures must never disconnect a healthy account or leak raw errors.
      const day = now().toISOString().slice(0, 10);
      if (warnedDay !== day) { warnedDay = day; warn('[whatsapp-health] lifecycle audit unavailable'); }
    }
  };
}
