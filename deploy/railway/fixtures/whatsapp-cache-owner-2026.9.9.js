// Upstream cache owner fixture from @openclaw/whatsapp 2026.9.9.
// Source: dist/.setup/monitor-CGpV2VNN.mjs; MIT License.
// Fixture extracted without changing the upstream cache or lifecycle code.
/*
MIT License

Copyright (c) 2026 OpenClaw Foundation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/
const WHATSAPP_BAILEYS_CACHE_MAX_ENTRIES = 500;
function rememberWhatsAppBaileysCacheEntry(cache, key, value, ttlMs) {
	if (!cache) return;
	if (cache.has(key)) cache.delete(key);
	cache.set(key, {
		expiresAt: Date.now() + ttlMs,
		value
	});
	pruneMapToMaxSize(cache, WHATSAPP_BAILEYS_CACHE_MAX_ENTRIES);
}
function readWhatsAppBaileysCacheEntry(cache, key) {
	const entry = cache.get(key);
	if (!entry) return;
	if (entry.expiresAt <= Date.now()) {
		cache.delete(key);
		return;
	}
	cache.delete(key);
	cache.set(key, entry);
	return entry.value;
}
//#endregion
//#region extensions/whatsapp/src/inbound/group-metadata-cache.ts
const GROUP_META_TTL_MS = 3e5;
const WHATSAPP_GROUP_METADATA_CACHE_MAX_ENTRIES = 500;
function resolveGroupMetadataExpiresAt(nowRaw = Date.now()) {
	const now = asDateTimestampMs(nowRaw);
	return now === void 0 ? void 0 : resolveExpiresAtMsFromDurationMs(GROUP_META_TTL_MS, { nowMs: now });
}
function rememberGroupMetadataCacheEntry(cache, jid, entry) {
	if (asDateTimestampMs(entry.expires) === void 0) {
		cache.delete(jid);
		return;
	}
	if (cache.has(jid)) cache.delete(jid);
	cache.set(jid, entry);
	pruneMapToMaxSize(cache, WHATSAPP_GROUP_METADATA_CACHE_MAX_ENTRIES);
}
function readGroupMetadataCacheEntry(cache, jid) {
	const entry = cache.get(jid);
	if (!entry) return null;
	const now = asDateTimestampMs(Date.now());
	const expires = asDateTimestampMs(entry.expires);
	if (now === void 0 || expires === void 0 || expires <= now) {
		cache.delete(jid);
		return null;
	}
	cache.delete(jid);
	cache.set(jid, entry);
	return entry;
}
function createWhatsAppGroupMetadataCacheOwner(params) {
	const reconnectCache = params.reconnectCache ?? /* @__PURE__ */ new Map();
	const localCache = /* @__PURE__ */ new Map();
	const groupMetadataGenerations = /* @__PURE__ */ new Map();
	const detachListeners = [];
	let closed = false;
	let started = false;
	const summarize = async (meta) => {
		const participantEntries = await Promise.all(meta.participants?.map(async (participant) => {
			const mapped = await params.resolveInboundJid(participant.id);
			return {
				display: mapped ?? participant.id,
				mention: {
					id: participant.id,
					lid: participant.lid,
					phoneNumber: participant.phoneNumber,
					e164: mapped
				}
			};
		}) ?? []);
		return {
			subject: meta.subject,
			participants: participantEntries.map((entry) => entry.display).filter(Boolean),
			mentionParticipants: participantEntries.map((entry) => entry.mention),
			expires: resolveGroupMetadataExpiresAt() ?? 0
		};
	};
	const summarizeForReconnect = (meta) => ({
		subject: meta.subject,
		expires: resolveGroupMetadataExpiresAt() ?? NaN
	});
	const rememberFullUpdate = (jid, meta) => {
		if (closed) return;
		groupMetadataGenerations.set(jid, {});
		rememberWhatsAppBaileysCacheEntry(params.baileysCache, jid, meta, GROUP_META_TTL_MS);
		rememberGroupMetadataCacheEntry(reconnectCache, jid, summarizeForReconnect(meta));
		localCache.delete(jid);
	};
	const forgetFullMetadata = (jid) => {
		groupMetadataGenerations.set(jid, {});
		params.baileysCache?.delete(jid);
		reconnectCache.delete(jid);
		localCache.delete(jid);
	};
	const get = async (jid) => {
		for (;;) {
			if (closed) return { expires: resolveGroupMetadataExpiresAt() ?? 0 };
			const cached = readGroupMetadataCacheEntry(localCache, jid);
			if (cached) return cached;
			const generation = groupMetadataGenerations.get(jid);
			try {
				const hydratedEntry = params.baileysCache?.get(jid);
				const providerMetadata = params.baileysCache ? readWhatsAppBaileysCacheEntry(params.baileysCache, jid) : void 0;
				const hydratedMetadata = providerMetadata?.participants?.length ? providerMetadata : void 0;
				const meta = hydratedMetadata ?? await (params.getCurrentSock() ?? params.sock).groupMetadata(jid);
				if (closed || groupMetadataGenerations.get(jid) !== generation) continue;
				const entry = await summarize(meta);
				if (closed || groupMetadataGenerations.get(jid) !== generation) continue;
				if (hydratedMetadata && hydratedEntry) entry.expires = hydratedEntry.expiresAt;
				else rememberWhatsAppBaileysCacheEntry(params.baileysCache, jid, meta, GROUP_META_TTL_MS);
				groupMetadataGenerations.set(jid, {});
				rememberGroupMetadataCacheEntry(reconnectCache, jid, {
					subject: entry.subject,
					expires: entry.expires
				});
				rememberGroupMetadataCacheEntry(localCache, jid, entry);
				return entry;
			} catch (error) {
				if (closed || groupMetadataGenerations.get(jid) !== generation) continue;
				const hydrated = readGroupMetadataCacheEntry(reconnectCache, jid);
				if (hydrated) {
					rememberGroupMetadataCacheEntry(localCache, jid, hydrated);
					params.logVerbose(`Using cached group metadata for ${jid} after fetch failure: ${String(error)}`);
					return hydrated;
				}
				params.logVerbose(`Failed to fetch group metadata for ${jid}: ${String(error)}`);
				return { expires: resolveGroupMetadataExpiresAt() ?? 0 };
			}
		}
	};
	const resolveOutboundMentions = async (jid, text) => {
		if (isJidGroup$1(jid) !== true || !mayContainWhatsAppOutboundMention(text)) return {
			text,
			mentionedJids: []
		};
		const meta = await get(jid);
		return resolveWhatsAppOutboundMentions({
			chatJid: jid,
			text,
			participants: meta.mentionParticipants
		});
	};
	const applyOutboundMentions = async (jid, content) => {
		if ("text" in content && typeof content.text === "string") {
			const resolved = await resolveOutboundMentions(jid, content.text);
			return addWhatsAppOutboundMentionsToContent({
				...content,
				text: resolved.text
			}, resolved.mentionedJids);
		}
		const caption = content.caption;
		if (typeof caption === "string") {
			const resolved = await resolveOutboundMentions(jid, caption);
			return addWhatsAppOutboundMentionsToContent({
				...content,
				caption: resolved.text
			}, resolved.mentionedJids);
		}
		return content;
	};
	const start = () => {
		if (started || closed) return;
		started = true;
		const listen = (event, listener) => {
			detachListeners.push(params.listen(event, listener));
		};
		listen("groups.upsert", (groups) => {
			for (const group of groups) if (group.id) rememberFullUpdate(group.id, group);
		});
		listen("groups.update", (updates) => {
			for (const update of updates) {
				if (!update.id) continue;
				if (typeof update.subject === "string" && Array.isArray(update.participants)) {
					rememberFullUpdate(update.id, update);
					continue;
				}
				forgetFullMetadata(update.id);
			}
		});
		listen("group-participants.update", (update) => {
			forgetFullMetadata(update.id);
		});
		(async () => {
			try {
				const groups = await params.sock.groupFetchAllParticipating();
				if (closed) return;
				for (const [jid, meta] of Object.entries(groups ?? {})) if (meta && !groupMetadataGenerations.has(jid)) {
					rememberGroupMetadataCacheEntry(reconnectCache, jid, summarizeForReconnect(meta));
					rememberWhatsAppBaileysCacheEntry(params.baileysCache, jid, meta, GROUP_META_TTL_MS);
					groupMetadataGenerations.set(jid, {});
				}
				params.logVerbose(`Hydrated ${Object.keys(groups ?? {}).length} participating groups on connect`);
			} catch (error) {
				const formatted = String(error);
				params.logHydrationWarning(formatted);
				params.logVerbose(`Failed to hydrate participating groups on connect: ${formatted}`);
			}
		})();
	};
	const close = () => {
		closed = true;
		for (const detach of detachListeners.splice(0)) detach();
	};
	return {
		start,
		close,
		get,
		resolveOutboundMentions,
		applyOutboundMentions
	};
}
//#endregion
