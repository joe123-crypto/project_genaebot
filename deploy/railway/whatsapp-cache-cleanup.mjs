// Only reclaim entries the adapter's existing cache readers already reject.
// Keep valid metadata, expiry times, reconnect fallbacks and fetch policy intact.
export function startExpiredWhatsAppCacheCleanup(caches, {
  now = Date.now,
  setIntervalFn = setInterval,
  clearIntervalFn = clearInterval,
} = {}) {
  let stopped = false;
  const sweep = () => {
    if (stopped) return;
    const timestamp = now();
    if (!Number.isFinite(timestamp)) return;
    for (const [cache, expiryKey] of caches) {
      if (!cache) continue;
      for (const [key, entry] of cache) {
        const expires = entry?.[expiryKey];
        if (Number.isFinite(expires) && expires <= timestamp) cache.delete(key);
      }
    }
  };
  sweep();
  const timer = setIntervalFn(sweep, 60_000);
  timer.unref?.();
  return () => {
    if (stopped) return;
    stopped = true;
    clearIntervalFn(timer);
  };
}
