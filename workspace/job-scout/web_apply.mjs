import dns from "node:dns/promises";
import fs from "node:fs/promises";
import net from "node:net";

const BLOCKED_HOSTS = [/(^|\.)linkedin\.com$/i];
const CAPTCHA_RE = /captcha|recaptcha|hcaptcha|verify you are human|cloudflare turnstile/i;
const LOGIN_RE = /sign\s*in|log\s*in|create (?:an )?account|register/i;
const PAYMENT_RE = /credit card|debit card|card number|payment|application fee/i;
const LEGAL_RE = /certify|declare|under penalty|legally authorized|work authorization|criminal|disability|veteran|gender|race|salary|compensation/i;
const SUCCESS_RE = /application (?:was |has been )?(?:submitted|received)|thank you for applying|submission (?:complete|received)|successfully applied/i;
const APPLY_RE = /^(?:easy )?apply(?: now)?$|submit application|continue application/i;

function privateIp(address) {
  if (net.isIPv4(address)) {
    const [a, b] = address.split(".").map(Number);
    return a === 10 || a === 127 || a === 0 || (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168);
  }
  if (net.isIPv6(address)) {
    const value = address.toLowerCase();
    return value === "::1" || value.startsWith("fe80:") || value.startsWith("fc") || value.startsWith("fd");
  }
  return true;
}

export async function validateExternalUrl(rawUrl) {
  const url = new URL(String(rawUrl || ""));
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname) throw new Error("invalid_application_url");
  if (BLOCKED_HOSTS.some((pattern) => pattern.test(url.hostname))) {
    return { url, blockerCode: "login_required" };
  }
  if (net.isIP(url.hostname) && privateIp(url.hostname)) throw new Error("private_application_url");
  const addresses = await dns.lookup(url.hostname, { all: true });
  if (!addresses.length || addresses.some(({ address }) => privateIp(address))) throw new Error("private_application_url");
  return { url, blockerCode: null };
}

function compact(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function evidenceUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    url.username = "";
    url.password = "";
    url.search = "";
    url.hash = "";
    return url.href;
  } catch {
    return "";
  }
}

function fieldKind(meta) {
  const hint = `${meta.autocomplete} ${meta.name} ${meta.id} ${meta.label} ${meta.placeholder}`.toLowerCase();
  if (PAYMENT_RE.test(hint)) return "payment";
  if (LEGAL_RE.test(hint)) return "legal";
  if (/cover.?letter|motivation/.test(hint)) return meta.type === "file" ? "coverLetterFile" : "coverLetter";
  if (/resume|résumé|curriculum|\bcv\b/.test(hint)) return "cv";
  if (meta.type === "file") return "unknownFile";
  if (/first.?name|given.?name/.test(hint)) return "firstName";
  if (/last.?name|family.?name|surname/.test(hint)) return "lastName";
  if (/full.?name|applicant.?name|your.?name/.test(hint) || meta.autocomplete === "name") return "fullName";
  if (meta.type === "email" || /e.?mail/.test(hint)) return "email";
  if (meta.type === "tel" || /phone|mobile|telephone|whatsapp/.test(hint)) return "phone";
  if (/location|city|town/.test(hint)) return "location";
  if (/privacy|data protection|processing of (?:my )?data/.test(hint) && meta.type === "checkbox") return "privacy";
  return null;
}

export function classifyField(meta) {
  return fieldKind({
    type: compact(meta.type).toLowerCase(),
    name: compact(meta.name),
    id: compact(meta.id),
    label: compact(meta.label),
    placeholder: compact(meta.placeholder),
    autocomplete: compact(meta.autocomplete).toLowerCase(),
  });
}

export async function probeChromium(chromium) {
  const browser = await chromium.launch({ headless: true });
  let context;
  try {
    context = await browser.newContext();
    const page = await context.newPage();
    await page.goto("data:text/html,<title>job-scout-ready</title><p>ready</p>", { waitUntil: "load", timeout: 10000 });
    if (await page.title() !== "job-scout-ready") throw new Error("chromium readiness page did not render");
    return true;
  } finally {
    await context?.close().catch(() => {});
    await browser.close().catch(() => {});
  }
}

async function visibleBody(page) {
  return compact(await page.locator("body").innerText({ timeout: 5000 }).catch(() => ""));
}

async function detectBlocker(page) {
  const body = await visibleBody(page);
  if (CAPTCHA_RE.test(body) || await page.locator('iframe[src*="captcha" i], [class*="captcha" i], [id*="captcha" i]').count()) return "captcha_required";
  if (PAYMENT_RE.test(body)) return "payment_required";
  if (await page.locator('input[type="password"]:visible').count()) return "login_required";
  return null;
}

async function followApplyCta(page) {
  if (await page.locator('form:visible input, form:visible textarea, form:visible select').count()) return;
  const candidates = page.getByRole("link").or(page.getByRole("button"));
  const count = Math.min(await candidates.count(), 80);
  for (let index = 0; index < count; index += 1) {
    const candidate = candidates.nth(index);
    const text = compact(await candidate.innerText().catch(() => ""));
    if (APPLY_RE.test(text)) {
      await candidate.click();
      await page.waitForLoadState("domcontentloaded", { timeout: 15000 }).catch(() => {});
      return;
    }
  }
}

async function fieldMetadata(locator) {
  return locator.evaluate((element) => {
    const id = element.getAttribute("id") || "";
    const explicit = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`)?.textContent : "";
    const wrapping = element.closest("label")?.textContent || "";
    return {
      type: element.getAttribute("type") || element.tagName.toLowerCase(),
      name: element.getAttribute("name") || "",
      id,
      label: explicit || wrapping || element.getAttribute("aria-label") || "",
      placeholder: element.getAttribute("placeholder") || "",
      autocomplete: element.getAttribute("autocomplete") || "",
      required: element.required || element.getAttribute("aria-required") === "true",
      disabled: element.disabled,
    };
  });
}

async function fillForm(page, payload) {
  const coverLetter = await fs.readFile(payload.coverLetterPath, "utf8");
  const values = { ...payload.applicant, coverLetter };
  const controls = page.locator('input:not([type="hidden"]), textarea, select');
  const count = await controls.count();
  const fileCount = await page.locator('input[type="file"]:visible').count();
  const filledFields = [];
  const unknownRequired = [];
  for (let index = 0; index < count; index += 1) {
    const control = controls.nth(index);
    if (!await control.isVisible().catch(() => false)) continue;
    const meta = await fieldMetadata(control);
    if (meta.disabled) continue;
    let kind = classifyField(meta);
    if (kind === "unknownFile" && fileCount === 1) kind = "cv";
    if (kind === "payment") return { blockerCode: "payment_required", filledFields };
    if (kind === "legal") return { blockerCode: "legal_declaration", filledFields };
    if (kind === "cv" || kind === "coverLetterFile") {
      await control.setInputFiles(kind === "cv" ? payload.cvPath : payload.coverLetterPath);
      filledFields.push(kind);
    } else if (kind === "privacy") {
      await control.check();
      filledFields.push(kind);
    } else if (kind && values[kind]) {
      if (meta.type === "select") await control.selectOption({ label: values[kind] }).catch(() => control.selectOption(values[kind]));
      else await control.fill(values[kind]);
      filledFields.push(kind);
    } else if (meta.required && !["submit", "button"].includes(meta.type)) {
      unknownRequired.push(compact(meta.label || meta.name || meta.placeholder || meta.id || "required field"));
    }
  }
  return { blockerCode: unknownRequired.length ? "unknown_required_field" : null, filledFields, unknownRequired };
}

async function submitAndConfirm(page) {
  const submit = page.locator('button[type="submit"], input[type="submit"]').first();
  if (!await submit.count()) return { outcome: "action_required", blockerCode: "unsupported_site" };
  await submit.click();
  await page.waitForLoadState("domcontentloaded", { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(1500);
  const body = await visibleBody(page);
  const confirmation = body.match(SUCCESS_RE)?.[0] || "";
  if (!confirmation) return { outcome: "failed", blockerCode: "confirmation_missing", error: "submission confirmation was not detected" };
  return { outcome: "submitted", confirmation: compact(confirmation).slice(0, 200) };
}

export async function applyToWebsite(payload, chromium) {
  const checked = await validateExternalUrl(payload.applicationUrl);
  if (checked.blockerCode) return { outcome: "action_required", blockerCode: checked.blockerCode, finalUrl: checked.url.href };
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ acceptDownloads: false });
  const hostSafety = new Map();
  await context.route("**/*", async (route) => {
    const requestUrl = route.request().url();
    if (!requestUrl.startsWith("http://") && !requestUrl.startsWith("https://")) return route.continue();
    try {
      const hostname = new URL(requestUrl).hostname;
      let safe = hostSafety.get(hostname);
      if (safe === undefined) {
        const result = await validateExternalUrl(requestUrl);
        safe = !result.blockerCode;
        hostSafety.set(hostname, safe);
      }
      return safe ? route.continue() : route.abort("blockedbyclient");
    } catch {
      return route.abort("blockedbyclient");
    }
  });
  const page = await context.newPage();
  try {
    await page.goto(checked.url.href, { waitUntil: "domcontentloaded", timeout: 30000 });
    let blockerCode = await detectBlocker(page);
    if (blockerCode) return { outcome: "action_required", blockerCode, finalUrl: page.url() };
    await followApplyCta(page);
    blockerCode = await detectBlocker(page);
    if (blockerCode) return { outcome: "action_required", blockerCode, finalUrl: page.url() };
    const form = await fillForm(page, payload);
    if (form.blockerCode) {
      return { outcome: "action_required", ...form, finalUrl: page.url() };
    }
    if (!payload.autoSubmit) return { outcome: "action_required", blockerCode: "approval_required", ...form, finalUrl: page.url() };
    const result = await submitAndConfirm(page);
    return {
      ...result,
      finalUrl: page.url(),
      evidence: { finalUrl: evidenceUrl(page.url()), filledFields: [...new Set(form.filledFields)].sort() },
    };
  } finally {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
  }
}
