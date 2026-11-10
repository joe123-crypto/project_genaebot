"""Bounded display cache; never used by writers or campaign sends.

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

TTL_SECONDS = 180
FAILURE_TTL_SECONDS = 300
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
        [config.API_ROOT, creds, env.get("GENAIE_CONSOLE_CONFIG_GENERATION", ""), env.get("GENAIE_CAMPAIGN_DELETION", "")],
        sort_keys=True).encode()).hexdigest()


def _epoch(directory):
    return config._read_json(os.path.join(directory, EPOCH_FILE))


def _cached(entry, identity, epoch, now):
    if not isinstance(entry, dict) or entry.get("identity") != identity or entry.get("epoch") != epoch:
        return None
    checked = entry.get("checkedAt")
    result = entry.get("result")
    ttl = FAILURE_TTL_SECONDS if entry.get("error") or isinstance(result, dict) and result.get("degraded") else TTL_SECONDS
    metadata = entry.get("metadata")
    delay = metadata.get("retryAfterSeconds", 0) if isinstance(metadata, dict) else 0
    if isinstance(delay, (int, float)) and 0 <= delay <= 86400:
        ttl = max(ttl, delay)
    if not isinstance(checked, (int, float)) or not 0 <= now() - checked < ttl:
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


def _project(result, *, campaign=None, group_ids=None, include_groups=True, include_campaigns=True):
    """Derive a narrower display without changing the original cache or its age."""
    membership = dict(result["membership"])
    groups = membership.get("groups") or {}
    if not include_groups:
        groups = {}
    elif group_ids is not None:
        groups = {jid: groups[jid] for jid in group_ids if jid in groups}
    elif campaign is not None:
        groups = {jid: row for jid, row in groups.items()
                  if campaign in (row.get("promos") or []) or campaign in (row.get("weekly") or {})}
    membership["groups"] = groups
    if not include_campaigns:
        membership["promos"] = {}
    return dict(result, membership=membership, enabled=result["enabled"] if include_campaigns else {})


def load(env=None, *, fresh=False, now=None, log=None, campaign=None, group_ids=None,
         include_groups=True, include_campaigns=True, **inject):
    env = os.environ if env is None else env
    now = now or time.time
    log = log or config._log
    scope = {"campaign": campaign, "group_ids": group_ids, "include_groups": include_groups, "include_campaigns": include_campaigns}
    try:
        identity = _identity(env)
    except config.ConfigUnavailable:
        # An unreadable credential cannot identify a display-cache entry.
        # The normal loader still validates and filters outage snapshots;
        # strict save reads must require a successful live read.
        return config.load(env, now=now, log=log, allow_snapshot=not fresh, **scope, **inject)
    directory = config.scope_directory(env, **scope)
    # Every scope watches the same mutation marker, including locally-run writes.
    epoch_directory = config.state_dir(env)
    path = os.path.join(directory, CACHE_FILE)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        epoch = _epoch(epoch_directory)
        if not fresh:
            # A healthy full-directory read already contains every narrower
            # display. Never use outage data or extend its TTL by copying it.
            if directory != epoch_directory:
                full = config._read_json(os.path.join(epoch_directory, CACHE_FILE))
                if isinstance(full, dict) and not full.get("error"):
                    try:
                        broader = _cached(full, identity, epoch, now)
                    except config.ConfigUnavailable:
                        broader = None
                    if broader is not None and not broader.get("degraded"):
                        return _project(broader, **scope)
            hit = _cached(config._read_json(path), identity, epoch, now)
            if hit is not None:
                return hit
        entry = {"identity": identity, "epoch": epoch}
        try:
            result = config.load(env, now=now, log=log, allow_snapshot=not fresh, **scope, **inject)
        except config.ConfigUnavailable as exc:
            entry.update(error=str(exc), metadata=exc.metadata)
            # A strict save read must fail, while background displays may
            # continue using the validated snapshot during the cooldown.
            import campaign_deletion
            if not campaign_deletion.enabled(env):
                try:
                    entry["result"] = config.scope_snapshot(env, str(exc), now=now, **scope)
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
            if _epoch(epoch_directory) == epoch:
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


def cached_summary(name, reader, env=None, *, now=None):
    """Cache a small aggregate without loading membership just to obtain counts."""
    env = os.environ if env is None else env
    now = now or time.time
    directory = config.state_dir(env)
    identity = _identity(env)
    digest = hashlib.sha256((name + identity).encode()).hexdigest()
    path = os.path.join(directory, "summaries", digest + ".json")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        epoch = _epoch(directory)
        entry = config._read_json(path)
        if isinstance(entry, dict) and entry.get("epoch") == epoch:
            ttl = FAILURE_TTL_SECONDS if entry.get("error") else TTL_SECONDS
            metadata = entry.get("metadata")
            delay = metadata.get("retryAfterSeconds", 0) if isinstance(metadata, dict) else 0
            if isinstance(delay, (int, float)) and 0 <= delay <= 86400:
                ttl = max(ttl, delay)
            checked = entry.get("checkedAt")
            if isinstance(checked, (int, float)) and 0 <= now() - checked < ttl:
                if entry.get("error"):
                    raise config.ConfigUnavailable(entry["error"], **entry.get("metadata", {}))
                if isinstance(entry.get("result"), dict):
                    return entry["result"]
        entry = {"epoch": epoch}
        try:
            entry["result"] = reader()
            return entry["result"]
        except config.ConfigUnavailable as exc:
            entry.update(error=str(exc), metadata=exc.metadata)
            raise
        finally:
            entry["checkedAt"] = now()
            if _epoch(directory) == epoch:
                config._write_private_json(path, entry)
