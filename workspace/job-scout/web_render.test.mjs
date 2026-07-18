import assert from "node:assert/strict";
import test from "node:test";
import { MAX_RENDER_HTML_BYTES, renderHtmlToPdf } from "./web_apply.mjs";
import { createWorkerServer } from "./web_apply_server.mjs";

function fakeChromium({ invalidPdf = false } = {}) {
  const calls = [];
  return {
    calls,
    async launch(options) {
      calls.push(["launch", options]);
      return {
        async newContext(options) {
          calls.push(["context", options]);
          return {
            async route(pattern, handler) { calls.push(["route", pattern, handler]); },
            async newPage() {
              return {
                async setContent(html, options) { calls.push(["setContent", html, options]); },
                async emulateMedia(options) { calls.push(["media", options]); },
                async pdf(options) {
                  calls.push(["pdf", options]);
                  return invalidPdf ? Buffer.from("not a pdf") : Buffer.from("%PDF-1.7 exact");
                },
              };
            },
            async close() { calls.push(["context-close"]); },
          };
        },
        async close() { calls.push(["browser-close"]); },
      };
    },
  };
}

test("renders A4 PDF with scripts and every request disabled", async () => {
  const browser = fakeChromium();
  const pdf = await renderHtmlToPdf("<!doctype html><style>body{color:black}</style><p>CV</p>", browser);

  assert.equal(pdf.toString(), "%PDF-1.7 exact");
  assert.deepEqual(browser.calls.find(([name]) => name === "context")[1], {
    javaScriptEnabled: false,
    serviceWorkers: "block",
  });
  assert.equal(browser.calls.find(([name]) => name === "route")[1], "**/*");
  assert.deepEqual(browser.calls.find(([name]) => name === "pdf")[1], {
    format: "A4",
    printBackground: true,
    preferCSSPageSize: false,
    timeout: 30_000,
  });
});

test("rejects URLs, oversized HTML, and invalid rendered bytes", async () => {
  await assert.rejects(renderHtmlToPdf("https://example.com/cv", fakeChromium()), /raw_html_required/);
  await assert.rejects(renderHtmlToPdf("x".repeat(MAX_RENDER_HTML_BYTES + 1), fakeChromium()), /html_too_large/);
  await assert.rejects(renderHtmlToPdf("<p>CV</p>", fakeChromium({ invalidPdf: true })), /invalid_rendered_pdf/);
});

test("render endpoint requires bearer auth and a raw text/html body", async () => {
  const browser = fakeChromium();
  const server = createWorkerServer({ authToken: "secret", browserType: browser });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  try {
    const unauthorized = await fetch(`http://127.0.0.1:${port}/render-pdf`, {
      method: "POST",
      headers: { "content-type": "text/html" },
      body: "<p>CV</p>",
    });
    assert.equal(unauthorized.status, 401);

    const wrongType = await fetch(`http://127.0.0.1:${port}/render-pdf`, {
      method: "POST",
      headers: { authorization: "Bearer secret", "content-type": "application/json" },
      body: JSON.stringify({ url: "https://example.com" }),
    });
    assert.equal(wrongType.status, 415);

    const rendered = await fetch(`http://127.0.0.1:${port}/render-pdf`, {
      method: "POST",
      headers: { authorization: "Bearer secret", "content-type": "text/html; charset=utf-8" },
      body: "<!doctype html><p>CV</p>",
    });
    assert.equal(rendered.status, 200);
    assert.equal(rendered.headers.get("content-type"), "application/pdf");
    assert.equal(Buffer.from(await rendered.arrayBuffer()).toString(), "%PDF-1.7 exact");
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
