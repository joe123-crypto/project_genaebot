const GREETING_PATTERNS = [
  /^(?:hi|hello|hey|hie|yo|salam|salaam|bonjour|good\s+(?:morning|afternoon|evening)|gm|morning|afternoon|evening)[\s!.?]*$/i,
  /^(?:hi|hello|hey|hie|salam|salaam|bonjour)\s+(?:there|openclaw|genaie|service\s+desk|bot|assistant)[\s!.?]*$/i,
  /^(?:start|menu|help|services|service|options)[\s!.?]*$/i,
  /^(?:what\s+(?:can\s+you\s+do|services\s+do\s+you\s+(?:offer|provide))|what\s+do\s+you\s+(?:do|offer|provide))[\s!.?]*$/i,
];

export function isGreetingOrServiceMenuRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  if (!compact || compact.length > 120) return false;
  return GREETING_PATTERNS.some((pattern) => pattern.test(compact));
}

export function getServiceMenuSelection(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  const match = /^([1-5])[\s.)-]*$/.exec(compact);
  return match ? match[1] : "";
}

// Leaves an in-flight automated flow (the CV order). Deliberately narrow: words
// like "help" or "menu" are normal answers mid-interview and must not abandon it.
export function isStateResetRequest(text) {
  const compact = String(text ?? "").trim().replace(/\s+/g, " ");
  return /^(?:cancel|cancel\s+(?:it|this|the\s+cv|my\s+cv|order)|start\s+over|restart|exit|quit)[\s!.?]*$/i.test(compact);
}
