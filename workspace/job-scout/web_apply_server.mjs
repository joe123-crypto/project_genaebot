import http from "node:http";
import { chromium } from "playwright";
import { applyToWebsite, probeChromium } from "./web_apply.mjs";

const port = Number(process.env.JOB_SCOUT_BROWSER_PORT || 8787);
const token = String(process.env.JOB_SCOUT_BROWSER_TOKEN || "");
if (!token) throw new Error("JOB_SCOUT_BROWSER_TOKEN is required");
const READY_TTL_MS = 5 * 60 * 1000;
let lastReadyAt = 0;
let readinessPromise = null;

function send(response, status, payload) {
  response.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" });
  response.end(JSON.stringify(payload));
}

async function chromiumReady() {
  if (Date.now() - lastReadyAt < READY_TTL_MS) return true;
  if (!readinessPromise) {
    readinessPromise = probeChromium(chromium)
      .then(() => {
        lastReadyAt = Date.now();
        return true;
      })
      .finally(() => {
        readinessPromise = null;
      });
  }
  return readinessPromise;
}

const server = http.createServer(async (request, response) => {
  if (request.method === "GET" && request.url === "/health") return send(response, 200, { ok: true });
  if (request.method === "GET" && request.url === "/ready") {
    try {
      await chromiumReady();
      return send(response, 200, { ok: true, chromium: true });
    } catch {
      return send(response, 503, { ok: false, chromium: false });
    }
  }
  if (request.method !== "POST" || request.url !== "/apply") return send(response, 404, { ok: false });
  if (request.headers.authorization !== `Bearer ${token}`) return send(response, 401, { ok: false });
  let body = "";
  for await (const chunk of request) {
    body += chunk;
    if (body.length > 128_000) return send(response, 413, { ok: false });
  }
  try {
    const payload = JSON.parse(body);
    const result = await applyToWebsite(payload, chromium);
    return send(response, 200, result);
  } catch (error) {
    const code = String(error?.message || "website application failed").replace(/[^a-z0-9_-]/gi, "_").slice(0, 80);
    return send(response, 200, { outcome: "failed", blockerCode: code, error: "website application failed" });
  }
});

server.listen(port, "0.0.0.0");
