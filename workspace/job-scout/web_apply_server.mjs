import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { applyToWebsite, MAX_RENDER_HTML_BYTES, probeChromium, probePdfRendering, renderHtmlToPdf } from "./web_apply.mjs";

const port = Number(process.env.JOB_SCOUT_BROWSER_PORT || 8787);
const token = String(process.env.JOB_SCOUT_BROWSER_TOKEN || "");
const READY_TTL_MS = 5 * 60 * 1000;
let lastReadyAt = 0;
let readinessPromise = null;

function send(response, status, payload) {
  response.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" });
  response.end(JSON.stringify(payload));
}

function sendPdf(response, pdf) {
  response.writeHead(200, {
    "content-type": "application/pdf",
    "content-length": String(pdf.length),
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  response.end(pdf);
}

async function readBody(request, maxBytes) {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of request) {
    bytes += chunk.length;
    if (bytes > maxBytes) throw new Error("request_too_large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function chromiumReady() {
  if (Date.now() - lastReadyAt < READY_TTL_MS) return true;
  if (!readinessPromise) {
    readinessPromise = Promise.all([probeChromium(chromium), probePdfRendering(chromium)])
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

export function createWorkerServer({ authToken = token, browserType = chromium } = {}) {
  if (!authToken) throw new Error("JOB_SCOUT_BROWSER_TOKEN is required");
  return http.createServer(async (request, response) => {
    if (request.method === "GET" && request.url === "/health") return send(response, 200, { ok: true });
    if (request.method === "GET" && request.url === "/ready") {
      try {
        if (browserType === chromium) await chromiumReady();
        else await Promise.all([probeChromium(browserType), probePdfRendering(browserType)]);
        return send(response, 200, { ok: true, chromium: true, pdf: true });
      } catch {
        return send(response, 503, { ok: false, chromium: false, pdf: false });
      }
    }
    const postRoute = request.method === "POST" && ["/apply", "/render-pdf"].includes(request.url);
    if (!postRoute) return send(response, 404, { ok: false });
    if (request.headers.authorization !== `Bearer ${authToken}`) return send(response, 401, { ok: false });
    try {
      const maxBytes = request.url === "/render-pdf" ? MAX_RENDER_HTML_BYTES : 128_000;
      const body = await readBody(request, maxBytes);
      if (request.url === "/render-pdf") {
        if (!String(request.headers["content-type"] || "").toLowerCase().startsWith("text/html")) {
          return send(response, 415, { ok: false, error: "text_html_required" });
        }
        const pdf = await renderHtmlToPdf(body, browserType);
        return sendPdf(response, pdf);
      }
      const payload = JSON.parse(body);
      const result = await applyToWebsite(payload, browserType);
      return send(response, 200, result);
    } catch (error) {
      if (String(error?.message || "") === "request_too_large") return send(response, 413, { ok: false });
      if (request.url === "/render-pdf") {
        const code = String(error?.message || "pdf_render_failed").replace(/[^a-z0-9_-]/gi, "_").slice(0, 80);
        return send(response, 400, { ok: false, error: code });
      }
      const code = String(error?.message || "website application failed").replace(/[^a-z0-9_-]/gi, "_").slice(0, 80);
      return send(response, 200, { outcome: "failed", blockerCode: code, error: "website application failed" });
    }
  });
}

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isMain) createWorkerServer().listen(port, "0.0.0.0");
