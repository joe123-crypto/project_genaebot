"""Five-minute display cache; never used by writers or campaign sends.

All console-read processes on the volume share one cache and one flock. A
generation from the API and a local invalidation marker fence older reads out
after mutations, including writes whose acknowledgement was lost.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import time

import operator_config as config

TTL_SECONDS = 300
CACHE_FILE = "console-config.json"
EPOCH_FILE = "console-config-epoch.json"


def invalidate(env=None):
    """Fence in-flight readers without waiting for their network requests."""
    directory = config.state_dir(env)
    config._write_private_json(os.path.join(directory, EPOCH_FILE), secrets.token_hex(16))


def _identity(env):
    creds = config.credentials(env)
    # Hash the credential context; no private key is persisted in this cache.
    return hashlib.sha256(json.dumps(
        [config.API_ROOT, creds, env.get("GENAIE_CONSOLE_CONFIG_GENERATION", "")],
        sort_keys=True).encode()).hexdigest()


def _epoch(directory):
    return config._read_json(os.path.join(directory, EPOCH_FILE))


def _cached(entry, identity, epoch, now):
    if not isinstance(entry, dict) or entry.get("identity") != identity or entry.get("epoch") != epoch:
        return None
    checked = entry.get("checkedAt")
    if not isinstance(checked, (int, float)) or not 0 <= now() - checked < TTL_SECONDS:
        return None
    result = entry.get("result")
    if not isinstance(result, dict):
        if isinstance(entry.get("error"), str):
            metadata = entry.get("metadata")
            safe = {k: v for k, v in metadata.items() if k in (
                "upstreamStatus", "code", "quota", "retryAfterSeconds")} if isinstance(metadata, dict) else {}
            raise config.ConfigUnavailable(entry["error"], **safe)
        return None
    age = config._snapshot_age_seconds(result, now)
    if age is None or not 0 <= age <= config.SNAPSHOT_MAX_AGE_SECONDS:
        return None
    if not isinstance(result.get("membership"), dict) or not isinstance(result.get("enabled"), dict):
        return None
    if result.get("source") not in ("firestore", "snapshot") or "degraded" not in result:
        return None
    if result["degraded"] is not None and not isinstance(result["degraded"], str):
        return None
    try:
        clean, _ = config.quarantine(result["membership"])
        _, problems = config.campaign_switches(result["enabled"])
    except (config.ConfigInvalid, TypeError, ValueError):
        return None
    if clean != result["membership"] or problems:
        return None
    if not isinstance(result.get("report"), dict):
        return None
    return dict(result, source="snapshot")


def load(env=None, *, fresh=False, now=None, log=None, **inject):
    env = os.environ if env is None else env
    now = now or time.time
    log = log or config._log
    directory = config.state_dir(env)
    path = os.path.join(directory, CACHE_FILE)
    identity = _identity(env)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        epoch = _epoch(directory)
        if not fresh:
            hit = _cached(config._read_json(path), identity, epoch, now)
            if hit is not None:
                return hit
        entry = {"identity": identity, "epoch": epoch}
        try:
            result = config.load(env, now=now, log=log, allow_snapshot=not fresh, **inject)
        except config.ConfigUnavailable as exc:
            entry.update(error=str(exc), metadata=exc.metadata)
            # A strict save read must fail, while background displays may
            # continue using the validated snapshot during the cooldown.
            try:
                entry["result"] = config._from_snapshot(directory, str(exc), now=now)
            except config.ConfigUnavailable:
                pass
            raise
        else:
            entry["result"] = result
            return result
        finally:
            entry["checkedAt"] = now()
            # Invalidation does not take the flock: a mutation must never wait
            # for a slow read. A late old entry is also rejected on epoch lookup.
            if _epoch(directory) == epoch:
                try:
                    config._write_private_json(path, entry)
                except OSError:
                    log("could not persist the console config cache")


@contextlib.contextmanager
def mutation(env=None):
    """Invalidate local readers before and after any attempted mutation."""
    def fence():
        try:
            invalidate(env)
        except OSError:
            config._log("could not invalidate the console config cache")
    fence()
    try:
        yield
    finally:
        fence()
