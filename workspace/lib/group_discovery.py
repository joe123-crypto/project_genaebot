"""Read the durable WhatsApp metadata inventory, independently of ad targeting.

No observation is merged into membership, generated targets, or Firestore.
The scanner retains old observations. A console removal hides those observations
through a saved timestamp without editing the scanner's inventory.
"""
from contextlib import closing
import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import time

ACCOUNT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
GROUP = re.compile(r"\d+(?:-\d+)?@g\.us")
STALE_SECONDS = 2 * 60 * 60
MAX_BYTES = 8 * 1024 * 1024
ERRORS = {"lookup_failed", "lookup_timeout", "invalid_metadata"}


class DiscoveryUnavailable(Exception):
    pass


class ObservationChanged(Exception):
    pass


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("invalid observation time")
    result = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("observation time has no timezone")
    return result.timestamp()


def read_inventory(path, account_id):
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("inventory too large")
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("version") != 1 or doc.get("accountId") != account_id:
        raise ValueError("invalid inventory header")
    groups = doc.get("groups")
    if not isinstance(groups, dict):
        raise ValueError("invalid inventory groups")
    for name in ("lastAttemptAt", "lastSuccessAt"):
        if doc.get(name) is not None:
            timestamp(doc[name])
    if doc.get("lastError") is not None and doc["lastError"] not in ERRORS:
        raise ValueError("invalid scan status")
    for jid, entry in groups.items():
        if not GROUP.fullmatch(jid) or len(jid) > 128 or not isinstance(entry, dict):
            raise ValueError("invalid group")
        name = entry.get("name")
        if name is not None and (not isinstance(name, str) or len(name) > 512):
            raise ValueError("invalid group name")
        if name is not None:
            name.encode("utf-8")
        first, last = timestamp(entry.get("firstSeenAt")), timestamp(entry.get("lastSeenAt"))
        if first > last:
            raise ValueError("invalid observation order")
    return doc


def _runtime(env):
    return Path(env.get("OPENCLAW_STATE_DIR") or "/home/joseph/.openclaw") / "runtime"


def _removal_db(env):
    return _runtime(env) / "whatsapp-group-removals" / "cutoffs.sqlite3"


def _cutoffs(env, jid=None):
    path = _removal_db(env).absolute()
    try:
        if not path.exists():
            return {}
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            # A reader can arrive while the first write is creating the table.
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'removals'").fetchone():
                return {}
            if jid is not None:
                return dict(db.execute("SELECT jid, observed_through FROM removals WHERE jid = ?", (jid,)))
            return dict(db.execute("SELECT jid, observed_through FROM removals"))
    except (OSError, sqlite3.Error) as exc:
        raise DiscoveryUnavailable("Saved discovery removals are unavailable.") from exc


def remove(jid, observed_through, env=None):
    """Dismiss only the observations covered by the row the operator confirmed."""
    env = os.environ if env is None else env
    if not isinstance(jid, str) or len(jid) > 128 or not GROUP.fullmatch(jid):
        raise ValueError("A valid WhatsApp group ID is required.")
    if not isinstance(observed_through, str) or len(observed_through) > 40:
        raise ValueError("A valid discovery observation time is required.")
    try:
        cutoff = timestamp(observed_through)
    except (ValueError, OverflowError) as exc:
        raise ValueError("A valid discovery observation time is required.") from exc
    if _cutoffs(env, jid).get(jid, float("-inf")) >= cutoff:
        return
    current = _read({}, env, time.time(), {}, only_jid=jid)
    rows = current["discoveredGroups"]
    if not rows:
        if any(account["lastError"] == "inventory_unavailable"
               for account in current["groupDiscovery"]["accounts"]):
            raise DiscoveryUnavailable("The discovery inventory is unavailable.")
        return  # A leave or another removal can already have cleared the row.
    if timestamp(rows[0]["lastSeenAt"]) != cutoff:
        raise ObservationChanged("This group’s discovery observation changed. Review the updated row before removing it.")
    path = _removal_db(env)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS removals (jid TEXT PRIMARY KEY, observed_through REAL NOT NULL)")
            db.execute("""INSERT INTO removals VALUES (?, ?)
                          ON CONFLICT(jid) DO UPDATE SET
                          observed_through = MAX(removals.observed_through, excluded.observed_through)""",
                       (jid, cutoff))
    except (OSError, sqlite3.Error) as exc:
        raise DiscoveryUnavailable("The discovery removal could not be saved.") from exc


def read(membership, env=None, now=None):
    """Extra Groups API fields. Missing/bad inventories never break the roster."""
    env = os.environ if env is None else env
    return _read(membership, env, time.time() if now is None else now, _cutoffs(env))


def _read(membership, env, now, cutoffs, only_jid=None):
    enabled = env.get("GENAIE_GROUP_DISCOVERY") != "0"
    directory = _runtime(env) / "whatsapp-groups"
    configured = membership.get("groups") or {}
    account_ids = set((membership.get("defaults") or {}).get("accounts") or {})
    try:
        account_ids.update(p.stem for p in directory.glob("*.json"))
    except OSError:
        pass
    rows, statuses = {}, []
    for account_id in sorted(account_ids):
        if not ACCOUNT.fullmatch(account_id) or account_id in {"__proto__", "constructor", "prototype"}:
            continue
        health = {"accountId": account_id, "lastAttemptAt": None,
                  "lastSuccessAt": None, "lastError": None,
                  "status": "waiting" if enabled else "disabled"}
        try:
            inventory = read_inventory(directory / (account_id + ".json"), account_id)
        except FileNotFoundError:
            statuses.append(health)
            continue
        except (OSError, ValueError, TypeError, OverflowError):
            health.update(status="error" if enabled else "disabled", lastError="inventory_unavailable")
            statuses.append(health)
            continue
        for field in ("lastAttemptAt", "lastSuccessAt", "lastError"):
            health[field] = inventory.get(field)
        if enabled:
            if health["lastError"]:
                health["status"] = "error"
            elif health["lastSuccessAt"]:
                health["status"] = "stale" if now - timestamp(health["lastSuccessAt"]) > STALE_SECONDS else "ok"
        statuses.append(health)
        groups = inventory["groups"]
        observations = groups.items() if only_jid is None else (
            [(only_jid, groups[only_jid])] if only_jid in groups else [])
        for jid, entry in observations:
            if jid in configured:
                # Includes on-hold/admin-only/not-a-member and cleared promos.
                continue
            if jid in cutoffs and timestamp(entry["lastSeenAt"]) <= cutoffs[jid]:
                continue
            row = rows.setdefault(jid, {"jid": jid, "name": None, "accounts": [],
                                        "firstSeenAt": entry["firstSeenAt"], "lastSeenAt": entry["lastSeenAt"]})
            if timestamp(entry["firstSeenAt"]) < timestamp(row["firstSeenAt"]):
                row["firstSeenAt"] = entry["firstSeenAt"]
            if timestamp(entry["lastSeenAt"]) >= timestamp(row["lastSeenAt"]):
                row["lastSeenAt"] = entry["lastSeenAt"]
                if entry.get("name"):
                    row["name"] = entry["name"]
            elif not row["name"] and entry.get("name"):
                row["name"] = entry["name"]
            row["accounts"].append({"accountId": account_id, "firstSeenAt": entry["firstSeenAt"],
                                    "lastSeenAt": entry["lastSeenAt"]})
    return {"discoveredGroups": sorted(rows.values(), key=lambda row: ((row["name"] or "").casefold(), row["jid"])),
            "groupDiscovery": {"enabled": enabled, "accounts": statuses}}
