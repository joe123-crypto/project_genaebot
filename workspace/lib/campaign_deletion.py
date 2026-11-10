"""Permanent campaign retirement, with an audited barrier and resumable cleanup.

Only the console API holds write/R2 credentials. Broadcasters use their read key
for a fresh deletion check at submission, never the 48-hour targeting snapshot.
"""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import ad_assets
import operator_config as config
import operator_writer as writer
import promo_config

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
R2_VARS = ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
FLAG = "GENAIE_CAMPAIGN_DELETION"


def enabled(env=None):
    return (os.environ if env is None else env).get(FLAG) == "1"


def capability(env=None):
    env = os.environ if env is None else env
    reason = None
    if not enabled(env):
        reason = "Campaign deletion is switched off on the bot. Set GENAIE_CAMPAIGN_DELETION=1."
    elif promo_config.mode(env)[0] != "firestore":
        reason = "Campaign deletion requires live Firestore configuration."
    elif not all((env.get(key) or "").strip() for key in R2_VARS):
        reason = "Campaign deletion requires bucket-scoped R2 cleanup credentials on the bot console API."
    elif not re.fullmatch(r"[a-fA-F0-9]{32}", env["R2_ACCOUNT_ID"]):
        reason = "The bot R2 account ID is invalid."
    elif ad_assets.mode(env)[0] != "r2" or not ad_assets.base_url(env):
        reason = "Campaign deletion requires live R2 picture configuration."
    return {"delete": reason is None, **({"reason": reason} if reason else {})}


def valid_slug(slug):
    if not isinstance(slug, str) or not SLUG.fullmatch(slug) or slug == "new":
        raise writer.WriteRefused("invalid campaign ID", status=400)
    return slug


def deleted_slugs(raw):
    return {slug for slug, fields in raw.get("campaigns", [])
            if config.decode_fields(fields).get("deleted") is True}


def strip_membership(membership, deleted):
    """Do this before validation: pending assignments must not quarantine groups."""
    clean = copy.deepcopy(membership)
    for slug in deleted:
        clean.get("promos", {}).pop(slug, None)
    for group in (clean.get("groups") or {}).values():
        for field in ("promos", "paused_promos"):
            if field in group:
                group[field] = [slug for slug in group[field] if slug not in deleted]
        if "weekly" in group:
            group["weekly"] = {slug: days for slug, days in group["weekly"].items() if slug not in deleted}
    return clean


def live_campaign(slug, env=None):
    """Read a single document with the runner's read-only credential. No fallback."""
    env = os.environ if env is None else env
    creds = config.credentials(env)
    if not creds:
        raise config.ConfigUnavailable("cannot verify campaign deletion without read credentials")
    token = config.access_token(creds, config.token_cache_path(config.state_dir(env)),
                                opener=urllib.request.urlopen, signer=config.node_signer,
                                now=time.time, sleep=time.sleep)
    row = config._request(config.documents_url(creds["project_id"], "campaigns", valid_slug(slug)),
                          headers=config._auth(token), opener=urllib.request.urlopen,
                          sleep=time.sleep, missing_ok=True)
    return config.decode_fields(row.get("fields", {})) if row else {}


@contextmanager
def campaign_lock(slug, env=None, exclusive=False):
    valid_slug(slug)
    root = Path(config.state_dir(env)) / "runtime" / "campaign-lifecycle"
    root.mkdir(parents=True, exist_ok=True)
    with (root / (slug + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


@contextmanager
def submission(slug, env=None):
    """A deletion waits for an already submitted message, then bars the next one."""
    if not enabled(env):
        yield True
        return
    with campaign_lock(slug, env):
        yield live_campaign(slug, env).get("deleted") is not True


def signer():
    path = Path("/usr/local/lib/genaie/r2_sigv4.py")
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "deploy" / "r2_sigv4.py"
    spec = importlib.util.spec_from_file_location("campaign_r2_signer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def delete_object(key, env):
    """Exact key from a validated asset record, never a browser URL or prefix."""
    sig = signer()
    endpoint = "https://%s.r2.cloudflarestorage.com" % env["R2_ACCOUNT_ID"]
    url = sig.object_url(endpoint, env["R2_BUCKET"], key)
    digest = hashlib.sha256(b"").hexdigest()
    headers = sig.signed_headers(method="DELETE", url=url, region="auto",
        access_key=env["R2_ACCESS_KEY_ID"], secret_key=env["R2_SECRET_ACCESS_KEY"],
        payload_sha256=digest, headers={"host": urllib.parse.urlsplit(url).netloc,
                                      "x-amz-content-sha256": digest})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers, method="DELETE"), timeout=10):
            pass
    except urllib.error.HTTPError as ex:
        status = ex.code
        ex.close()
        if status != 404:
            raise config.ConfigUnavailable("R2 picture cleanup returned HTTP %d" % status) from ex


def remove_docs(store, collection, ids):
    if ids:
        store.request(":commit", {"writes": [{"delete": "%s/%s/%s" % (store.base, collection, key)} for key in ids],
                                  **({"transaction": store.transaction} if store.transaction else {})})
        store.transaction = None


def summary(slug, operation):
    return {"slug": slug, "name": operation.get("name", slug), "cleanup": operation["cleanup"],
            **({"detail": operation["detail"]} if operation.get("detail") else {})}


def delete(slug, actor, store, env=None):
    env = os.environ if env is None else env
    valid_slug(slug)
    support = capability(env)
    if not support["delete"]:
        raise writer.WriteRefused(support["reason"], status=409)
    # Shared with job creation: a reconcile started before the deletion must not
    # install a job after cleanup has marked the operation complete.
    with scheduler_lock(env), campaign_lock(slug, env, exclusive=True):
        store.begin()
        try:
            previous = store.get("campaign_deletions", slug)
            campaign = store.get("campaigns", slug)
            if previous:
                return summary(slug, previous)
            manifest = promo_config.read_manifest(env) or {}
            declared = next((row for row in manifest.get("promos", []) if row.get("slug") == slug), None)
            if not campaign and not declared:
                raise writer.WriteRefused("campaign does not exist", status=404)
            if campaign and campaign.get("deleted"):
                raise writer.WriteRefused("deletion marker has no cleanup receipt; operator review required", status=409)
            assets = [(key, config.decode_fields(fields)) for key, fields in store.list("assets")]
            owned = []
            for key, entry in assets:
                if entry.get("campaign") == slug:
                    checked = ad_assets._entry(key, entry)
                    # Do not retain a derived URL in the durable operation.
                    owned.append({"id": key, **{name: checked[name] for name in
                                  ("campaign", "filename", "key", "sha256", "bytes", "content_type", "rank")}})
            # Upload tools reserve immutable keys before PUT; a process dying
            # between its PUT and index commit must not leave retired pictures.
            for receipt, fields in store.list("campaign_asset_uploads"):
                for entry in config.decode_fields(fields).get("assets", []):
                    if entry.get("campaign") == slug:
                        checked = ad_assets._entry(entry["id"], entry)
                        if not any(asset["key"] == checked["key"] for asset in owned):
                            owned.append(checked)
            name = (campaign or {}).get("name") or (declared or {}).get("name") or slug
            before = copy.deepcopy(campaign or {})
            if declared:
                before["manifest"] = declared
            audit_id, audit = writer.audit_document(actor, "campaigns", slug, before, None)
            audit["assets"] = owned
            operation = {"name": name, "cleanup": "pending", "assets": owned,
                         "jobNames": list(dict.fromkeys(([declared["name"]] + declared.get("legacyNames", [])) if declared else ["console-%s-promo" % slug])),
                         "commands": ([{"argv": ["python3", declared["script"]]}] +
                            [{"argv": ["python3", str(Path(declared["script"]).parent.parent / legacy / Path(declared["script"]).name)]} for legacy in declared.get("legacyNames", [])])
                            if declared else [{"argv": ["python3", str(Path(__file__).resolve().parents[1] / "bin" / "run-console-campaign"), slug]}],
                         "audit": audit_id, "deletedAt": audit["at"], "picturesAfter": int(time.time()) + 600}
            if campaign and campaign.get("kind") == "console":
                import console_campaigns
                operation["jobNames"] = list(dict.fromkeys(operation["jobNames"] + [console_campaigns.job_name(slug, campaign)]))
                runner = ["python3", str(Path(__file__).resolve().parents[1] / "bin" / "run-console-campaign"), slug]
                if not any(command["argv"] == runner for command in operation["commands"]):
                    operation["commands"].append({"argv": runner})
            marker = {"deleted": True, "enabled": False, "deletedAt": audit["at"], "deletion": slug}
            with __import__("console_config").mutation(env):
                store.commit([("campaigns", slug, marker, campaign is not None),
                              ("campaign_deletions", slug, operation, False), ("audit", audit_id, audit, False)])
        finally:
            store.rollback()
    # Commit is the acceptance boundary; external work runs in the worker. A lost
    # HTTP reply is recovered by repeating this request with the same slug.
    return summary(slug, operation)


@contextmanager
def scheduler_lock(env=None):
    directory = Path(config.state_dir(env)) / "runtime"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "console-campaign-scheduler.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def cleanup(slug, store, env=None, call=None, remove_object=None):
    env = os.environ if env is None else env
    import console_campaigns
    call = call or console_campaigns.gateway
    remove_object = remove_object or delete_object
    with scheduler_lock(env), campaign_lock(slug, env, exclusive=True):
        operation = store.get("campaign_deletions", slug)
        if not operation:
            return
        original_operation = copy.deepcopy(operation)
        try:
            jobs = call(["list", "--all", "--json"]).get("jobs", [])
            for job in jobs:
                if job.get("name") in operation["jobNames"]:
                    if (job.get("payload") or {}).get("argv") not in [command["argv"] for command in operation["commands"]]:
                        raise config.ConfigUnavailable("a campaign job name has an unexpected command; operator review required")
                    call(["rm", job["id"]])
            if any(job.get("name") in operation["jobNames"] for job in call(["list", "--all", "--json"]).get("jobs", [])):
                raise config.ConfigUnavailable("campaign gateway jobs still exist")
            if operation["cleanup"] == "complete":
                return "complete"
            # One group per transaction preserves concurrent edits and cannot hit
            # Firestore's 500-write limit. Tombstones already remove it from reads.
            for jid, fields in store.list("groups"):
                store.begin()
                try:
                    group = store.get("groups", jid)
                    if group is None:
                        continue
                    clean = strip_membership({"groups": {jid: group}}, {slug})["groups"][jid]
                    if clean != group:
                        audit_id, audit = writer.audit_document("campaign cleanup", "groups", jid, group, clean, reason="Deleted campaign " + slug)
                        with __import__("console_config").mutation(env):
                            store.commit([("groups", jid, clean, True), ("audit", audit_id, audit, False)])
                finally:
                    store.rollback()
            # Browser PUT URLs last five minutes. The grace also covers a PUT
            # admitted just before expiry, so an old form cannot refill a key
            # immediately after cleanup. Fresh URL signing rejects retired IDs.
            if time.time() < operation["picturesAfter"]:
                raise config.ConfigUnavailable("waiting for existing picture upload URLs to expire")
            for receipt, fields in store.list("campaign_asset_uploads"):
                upload = config.decode_fields(fields)
                if upload.get("expiresAt", 0) > time.time() and any(asset.get("campaign") == slug for asset in upload.get("assets", [])):
                    raise config.ConfigUnavailable("waiting for a reserved picture upload to finish or expire")
            for asset in operation["assets"]:
                # Query inside a transaction first; supported upload/create paths
                # cannot add a reference to a retired campaign's immutable keys.
                store.begin()
                try:
                    references = [(key, config.decode_fields(fields)) for key, fields in store.list("assets")]
                    shared = any(entry.get("key") == asset["key"] and entry.get("campaign") != slug
                                 for key, entry in references)
                    remove_docs(store, "assets", [key for key, entry in references
                                                  if entry.get("campaign") == slug and entry.get("key") == asset["key"]])
                finally:
                    store.rollback()
                if not shared:
                    remove_object(asset["key"], env)
                cached = Path(ad_assets.cache_path(asset, env))
                cached.unlink(missing_ok=True)
            # A successful cleanup invalidates cached catalogues, which must not
            # make a deleted object's metadata visible again after a restart.
            cache_directory = Path(ad_assets.cache_dir(env)) / valid_slug(slug)
            if cache_directory.exists():
                shutil.rmtree(cache_directory)
            operation["cleanup"] = "complete"
            operation.pop("detail", None)
        except Exception as ex:
            operation["cleanup"] = "pending"
            operation["detail"] = "Cleanup is pending: %s. The bot will retry." % (str(ex) if isinstance(ex, (config.ConfigUnavailable, writer.WriteRefused)) else type(ex).__name__)
            sys.stderr.write("[campaign-deletion] %s cleanup pending: %s\n" % (slug, type(ex).__name__))
        if operation != original_operation:
            with __import__("console_config").mutation(env):
                store.commit([("campaign_deletions", slug, operation, True)])
        return operation["cleanup"]


def reconcile_all(store, env=None):
    return sum(cleanup(slug, store, env) == "pending" for slug, fields in store.list("campaign_deletions"))


def transactional_write(collection, doc_id, patch, actor, env, dry_run=False):
    """Use the same transaction protocol as deletion, so stale edits conflict."""
    import console_campaigns
    store = console_campaigns.Store(env)
    store.begin()
    try:
        raw = {name: store.list(name) for name in config.COLLECTIONS if name != "groups"}
        if collection == "groups":
            current = store.get("groups", doc_id)
            raw["groups"] = [(doc_id, config.encode_fields(current))] if current is not None else store.list("groups")
        else:
            raw["groups"] = store.list("groups")
        fields, before, _, report = writer.prepare(raw, collection, doc_id, patch)
        result = {"collection": collection, "id": doc_id, "fields": [], "before": before,
                  "after": fields, "warnings": list(report.get("warnings") or []), "audit": None}
        if fields == before:
            return dict(result, change="unchanged")
        audit_id, audit = writer.audit_document(actor, collection, doc_id, before, fields)
        result.update(change="created" if before is None else "updated", fields=audit["fields"])
        if not dry_run:
            store.commit([(collection, doc_id, fields, before is not None), ("audit", audit_id, audit, False)])
            result["audit"] = audit_id
        return result
    finally:
        store.rollback()
