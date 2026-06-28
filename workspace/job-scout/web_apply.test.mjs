import assert from "node:assert/strict";
import test from "node:test";
import { classifyField, probeChromium, validateExternalUrl } from "./web_apply.mjs";

test("classifies only fields backed by approved applicant data", () => {
  assert.equal(classifyField({ type: "email", label: "Email address" }), "email");
  assert.equal(classifyField({ type: "file", label: "Upload CV" }), "cv");
  assert.equal(classifyField({ type: "file", label: "Attachment" }), "unknownFile");
  assert.equal(classifyField({ type: "textarea", label: "Cover letter" }), "coverLetter");
  assert.equal(classifyField({ type: "checkbox", label: "Privacy and data processing" }), "privacy");
  assert.equal(classifyField({ type: "radio", label: "Are you legally authorized to work here?" }), "legal");
  assert.equal(classifyField({ type: "text", label: "Why should we hire you?" }), null);
});

test("blocks private and login-only destinations before browser launch", async () => {
  await assert.rejects(validateExternalUrl("http://127.0.0.1/internal"), /private_application_url/);
  const linkedIn = await validateExternalUrl("https://www.linkedin.com/jobs/view/1");
  assert.equal(linkedIn.blockerCode, "login_required");
});

test("readiness launches Chromium and verifies a rendered page", async () => {
  const calls = [];
  const chromium = {
    async launch(options) {
      calls.push(["launch", options]);
      return {
        async newContext() {
          calls.push(["context"]);
          return {
            async newPage() {
              return {
                async goto(url) { calls.push(["goto", url]); },
                async title() { return "job-scout-ready"; },
              };
            },
            async close() { calls.push(["context-close"]); },
          };
        },
        async close() { calls.push(["browser-close"]); },
      };
    },
  };

  assert.equal(await probeChromium(chromium), true);
  assert.deepEqual(calls.map(([name]) => name), ["launch", "context", "goto", "context-close", "browser-close"]);
});

test("readiness closes Chromium when rendering fails", async () => {
  let contextClosed = false;
  let browserClosed = false;
  const chromium = {
    async launch() {
      return {
        async newContext() {
          return {
            async newPage() {
              return { async goto() {}, async title() { return "wrong"; } };
            },
            async close() { contextClosed = true; },
          };
        },
        async close() { browserClosed = true; },
      };
    },
  };

  await assert.rejects(probeChromium(chromium), /did not render/);
  assert.equal(contextClosed, true);
  assert.equal(browserClosed, true);
});
