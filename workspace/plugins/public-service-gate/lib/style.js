// Em dashes read as machine-written to customers, so no outbound WhatsApp
// message may carry one. The prompt forbids them, but a prompt is a request:
// this rewrite is the guarantee. It runs on every outbound WhatsApp message and
// turns the dash into the punctuation a person would have typed instead.
//
// Links are left exactly as they are. A link must reach the customer character
// for character, so URLs are cut out before the rewrite and put back untouched.

const DASH_CLASS = "\\u2012\\u2013\\u2014\\u2015\\u2E3A\\u2E3B";
const HAS_DASH = new RegExp(`[${DASH_CLASS}]`);
// A dash between two numbers ("$1800 to $2400", "40 to 60") is a range, so it
// reads as "to" rather than a comma.
const NUMERIC_RANGE = new RegExp(`(\\d)[^\\S\\n]*[${DASH_CLASS}]+[^\\S\\n]*([$€£]?\\d)`, "g");
const DASH_RUN = new RegExp(`([^\\S\\n]*)[${DASH_CLASS}]+([^\\S\\n]*)`, "g");
const URL_RUN = /(?:https?:\/\/|www\.)\S+/gi;
const CLAUSE_PUNCTUATION = /[,;:.!?]/;
const PLACEHOLDER = "\u0001";

function rewriteDashes(text) {
  let out = text;
  let previous = null;
  // Chained ranges overlap on a shared digit ("20 to 30 to 40"), so settle them
  // first, until no more replacements land.
  while (out !== previous) {
    previous = out;
    out = out.replace(NUMERIC_RANGE, "$1 to $2");
  }
  out = out.replace(DASH_RUN, (match, before, after, offset, whole) => {
    const prev = whole[offset - 1];
    const next = whole[offset + match.length];
    // A dash opening or closing a line is decoration, not punctuation.
    if (prev === undefined || prev === "\n" || next === undefined || next === "\n") return "";
    if (!before && !after) {
      // A dash wedged between two letters was meant to be a hyphen ("pre-paid");
      // anything else just gets a gap.
      return /\w/.test(prev) && /\w/.test(next) ? "-" : " ";
    }
    // A dash already following a comma must not produce a doubled comma.
    return CLAUSE_PUNCTUATION.test(prev) ? " " : ", ";
  });
  return out
    .replace(/[^\S\n]{2,}/g, " ")
    .replace(/[^\S\n]+([,;:.!?])/g, "$1")
    .replace(/,(\s*\n)/g, "$1")
    .replace(/,\s*$/, "");
}

export function containsDash(text) {
  return HAS_DASH.test(String(text ?? ""));
}

/** Returns `text` with every em/en dash replaced, leaving URLs untouched. */
export function stripDashes(text) {
  // A control character cannot reach us from WhatsApp, so it is safe to stand
  // in for a URL while the rest of the message is rewritten around it.
  const source = String(text ?? "").split(PLACEHOLDER).join("");
  if (!containsDash(source)) return source;

  const urls = [];
  const masked = source.replace(URL_RUN, (url) => {
    urls.push(url);
    return `${PLACEHOLDER}${urls.length - 1}${PLACEHOLDER}`;
  });
  const out = rewriteDashes(masked).replace(
    new RegExp(`${PLACEHOLDER}(\\d+)${PLACEHOLDER}`, "g"),
    (_match, index) => urls[Number(index)],
  );

  // Never trade a reply for a scrub: an empty rewrite is suppressed delivery.
  return out.trim() ? out : source;
}
