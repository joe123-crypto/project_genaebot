export function normalizePhone(value) {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  if (!trimmed) return "";
  // WhatsApp can withhold a sender's phone number. Preserve its authenticated
  // direct-chat identity; stripping @lid would impersonate an unrelated phone.
  const lid = /^(\d{1,32})(?::\d+)?@(lid|hosted\.lid)$/.exec(trimmed);
  if (lid) return `${lid[1]}@${lid[2]}`;
  const phoneJid = /^(\d{8,15})(?::\d+)?@(s\.whatsapp\.net|hosted)$/.exec(trimmed);
  if (phoneJid) return `+${phoneJid[1]}`;
  if (trimmed.includes("@")) return "";
  const plus = trimmed.startsWith("+") ? "+" : "";
  return plus + trimmed.replace(/[^\d]/g, "");
}

export function getEventChannel(event, ctx = {}) {
  return String(event?.channel ?? ctx?.channelId ?? ctx?.channel ?? "").trim();
}

export function isWhatsAppChannel(value) {
  const channel = String(value ?? "").trim().toLowerCase();
  return channel === "whatsapp" || channel.startsWith("whatsapp:");
}

// True when an inbound message originates from a group chat rather than a 1:1 DM.
// Keys off the core-provided `isGroup` flag (set whether or not the bot is
// @-mentioned), with defensive fallbacks on peer.kind and a group JID (@g.us) so
// the check holds even if field naming shifts across openclaw versions.
export function isGroupEvent(event = {}, ctx = {}) {
  if (event.isGroup === true || ctx.isGroup === true) return true;
  const kind = event.peer?.kind ?? ctx.peer?.kind;
  if (kind === "group") return true;
  const jid = event.conversationId ?? ctx.conversationId ?? event.chatId ?? event.remoteJid ?? "";
  return typeof jid === "string" && jid.endsWith("@g.us");
}

export function textCandidate(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value
      .map((part) => {
        if (typeof part === "string") return part;
        if (typeof part?.text === "string") return part.text;
        if (typeof part?.content === "string") return part.content;
        return "";
      })
      .join(" ");
  }
  return "";
}

export function getEventText(event = {}) {
  const candidates = [
    event.content,
    event.body,
    event.text,
    event.message?.text,
    event.message?.body,
    event.message?.content,
    event.payload?.text,
    event.payload?.body,
    event.payload?.content,
  ];
  for (const candidate of candidates) {
    const text = textCandidate(candidate).trim();
    if (text) return text;
  }
  return "";
}

export function getEventSender(event = {}, ctx = {}) {
  return normalizePhone(
    event.senderId ??
      event.sender_id ??
      event.from ??
      event.sender?.e164 ??
      event.sender?.id ??
      event.sender?.phone ??
      event.message?.from ??
      event.payload?.from ??
      ctx.senderId ??
      ctx.sender_id ??
      ctx.from ??
      ctx.sender?.e164 ??
      ctx.sender?.id ??
      "",
  );
}

// Which WhatsApp line received this message ("default" / "ads"). Core puts it on
// the before_dispatch CONTEXT only (the event object has no account field), and
// it is typed optional, so callers must treat "" as unknown. The sibling-line
// guard needs it to tell admin self-chat from one bot line DMing the other.
export function getEventAccountId(event = {}, ctx = {}) {
  return String(
    ctx.accountId ??
      ctx.agentAccountId ??
      ctx.deliveryContext?.accountId ??
      ctx.account_id ??
      ctx.account?.id ??
      event.accountId ??
      event.account_id ??
      event.metadata?.accountId ??
      "",
  ).trim();
}

export function getOutboundRecipient(event = {}, ctx = {}) {
  return (
    event.to ??
    event.target ??
    event.recipient ??
    event.recipientId ??
    event.metadata?.to ??
    event.metadata?.recipient ??
    ctx.senderId ??
    ctx.sender_id ??
    ctx.from ??
    ctx.sender?.e164 ??
    ctx.sender?.id ??
    ""
  );
}

export function normalizeText(text) {
  return String(text ?? "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

export function parseIsoTime(value) {
  const text = String(value ?? "").trim();
  if (!text) return null;
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? null : date;
}
