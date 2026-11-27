#!/usr/bin/env python3
"""Move one workspace's campaign pictures into the workspace layout in R2.

Pictures used to sit under ``ads/<slug>/`` (the install that predates
workspaces) or ``w/<workspace>/ads/<slug>/`` (the first workspace bots). They
now sit where the workspace's documents do in Firestore:

    workspaces/<workspace>/campaigns/<slug>/<sha256>.<ext>

The ``ads`` in the old keys was only a folder name. It has nothing to do with
the WhatsApp line called ``ads``.

The bot reads a picture through the full key its index row names, so a move is
three things in order: copy the object, repoint the index, and only once
nothing names the old key, delete it. Each is its own mode, and the default
writes nothing:

    move-pictures-to-workspace-layout.py               the plan
    move-pictures-to-workspace-layout.py --write       copy, then repoint
    move-pictures-to-workspace-layout.py --verify      every row in the new layout, bytes right
    move-pictures-to-workspace-layout.py --delete-old  delete the old objects

``--write`` copies over the signed S3 API, checking each object's sha256
against its index row, then rewrites every ``assets/*.key`` and every key in
``campaigns/*.images`` in one transaction, with an audit record per document.
A copy is idempotent (the key is the content's digest), so a rerun after a
failure picks up where it stopped. ``campaign_deletions`` and ``audit`` are
history and keep the keys they recorded.

``--delete-old`` deletes a legacy object only when its copy in the new layout
holds the same bytes and no index row (of any campaign, deleted ones included,
in any workspace) names the old key. An object nothing ever named is reported
and left alone.

The bot's own cache is keyed by campaign, digest and filename, never by the
object key, so sends keep using their cached files throughout.

Needs GENAIE_WORKSPACE_ID, a Firestore key with Cloud Datastore User (--key or
GCP_SERVICE_ACCOUNT_FILE; the seed key), and the R2 settings that
upload-ad-image.py reads from ~/.config/genaie/r2-upload.env. --delete-old
needs a token that may delete objects.

Exit codes: 0 done / clean, 1 a check failed, 2 bad input or refused.
"""

import argparse
import copy
import hashlib
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT / "workspace" / "lib"), str(ROOT / "deploy")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import ad_assets  # noqa: E402
import operator_config  # noqa: E402
import operator_writer  # noqa: E402
import r2_sigv4  # noqa: E402

ACTOR = "move-pictures-to-workspace-layout"
REASON = "pictures moved to the workspace layout in R2"
MAX_WRITES = 500


class MoveError(Exception):
    pass


# ------------------------------------------------------------------ plan ----
def legacy_prefixes(workspace):
    """The two old shapes this workspace's pictures may still be under."""
    return ("ads/", "w/%s/ads/" % workspace)


def new_key(key, workspace):
    """``key`` in the workspace layout, keeping its campaign folder and name.

    ``None`` when it is already there. Another workspace's key is refused,
    because moving it would hand this workspace someone else's picture."""
    if not isinstance(key, str) or not ad_assets.KEY_RE.match(key):
        raise MoveError("%r is not a picture key" % (key,))
    target = "workspaces/%s/campaigns/" % workspace
    if key.startswith(target):
        return None
    for prefix in legacy_prefixes(workspace):
        if key.startswith(prefix):
            return target + key[len(prefix):]
    raise MoveError("%s belongs to another workspace" % key)


def decode(rows):
    return dict((doc_id, operator_config.decode_fields(fields)) for doc_id, fields in rows)


def plan(workspace, assets, campaigns):
    """What to copy and what to repoint, from decoded ``{id: doc}`` maps.

    Returns ``(moves, asset_changes, campaign_changes)``:

    * ``moves`` is ``{old_key: {"key": new, "sha256", "bytes", "content_type"}}``;
    * ``asset_changes`` is ``{doc_id: (before, after)}``;
    * ``campaign_changes`` is ``{slug: (before_images, after_images)}``.

    A deleted campaign's pictures are left to that deletion's own cleanup."""
    deleted = set(slug for slug, doc in campaigns.items() if doc.get("deleted") is True)
    moves, asset_changes, campaign_changes = {}, {}, {}

    def add(old, sha256, size, content_type, where):
        target = new_key(old, workspace)
        if target is None:
            return None
        move = {"key": target, "sha256": sha256, "bytes": size, "content_type": content_type}
        if moves.setdefault(old, move) != move:
            raise MoveError("%s: %s is named with two different digests or sizes" % (where, old))
        return target

    for doc_id, doc in sorted(assets.items()):
        if doc.get("campaign") in deleted:
            continue
        try:
            entry = ad_assets._entry(doc_id, doc)
        except ValueError as ex:
            raise MoveError("assets/%s is not a usable index row: %s" % (doc_id, ex))
        target = add(entry["key"], entry["sha256"], entry["bytes"], entry["content_type"],
                     "assets/%s" % doc_id)
        if target:
            asset_changes[doc_id] = (doc, dict(doc, key=target))
    for slug, doc in sorted(campaigns.items()):
        if slug in deleted or not doc.get("images"):
            continue
        images = copy.deepcopy(doc["images"])
        changed = False
        for image in images:
            if not isinstance(image, dict):
                raise MoveError("campaigns/%s.images holds %r" % (slug, image))
            target = add(image.get("key"), image.get("sha256"), image.get("bytes"),
                         image.get("contentType"), "campaigns/%s" % slug)
            if target:
                image["key"] = target
                changed = True
        if changed:
            campaign_changes[slug] = (doc["images"], images)
    return moves, asset_changes, campaign_changes


def active_leases(uploads, now):
    """Upload receipts still inside their lease. A browser or script holding one
    may be about to index a key in the old layout, so nothing moves meanwhile."""
    return sorted(receipt for receipt, doc in uploads.items()
                  if isinstance(doc.get("expiresAt"), (int, float)) and doc["expiresAt"] > now)


def commit_writes(base, workspace, asset_changes, campaign_changes, now=None):
    """The one transaction's writes: each changed field by mask, each guarded by
    the document existing, and one audit record per document."""
    writes = []
    for doc_id, (before, after) in sorted(asset_changes.items()):
        writes.append({"update": {"name": "%s/%s/%s" % (base, ad_assets.COLLECTION, doc_id),
                                  "fields": operator_config.encode_fields({"key": after["key"]})},
                       "updateMask": {"fieldPaths": ["key"]},
                       "currentDocument": {"exists": True}})
        writes.append(audit(base, ad_assets.COLLECTION, doc_id, {"key": before["key"]},
                            {"key": after["key"]}, now))
    for slug, (before, after) in sorted(campaign_changes.items()):
        writes.append({"update": {"name": "%s/campaigns/%s" % (base, slug),
                                  "fields": operator_config.encode_fields({"images": after})},
                       "updateMask": {"fieldPaths": ["images"]},
                       "currentDocument": {"exists": True}})
        writes.append(audit(base, "campaigns", slug, {"images": before}, {"images": after}, now))
    if len(writes) > MAX_WRITES:
        raise MoveError("%d writes exceed Firestore's atomic limit of %d" % (len(writes), MAX_WRITES))
    return writes


def audit(base, collection, doc_id, before, after, now):
    entry_id, entry = operator_writer.audit_document(ACTOR, collection, doc_id, before, after,
                                                     now=now, reason=REASON)
    return {"update": {"name": "%s/%s/%s" % (base, operator_writer.AUDIT_COLLECTION, entry_id),
                       "fields": operator_config.encode_fields(entry)},
            "currentDocument": {"exists": False}}


def named_keys(assets, campaigns):
    """Every key any row names: index rows of every campaign, deleted ones
    included, and every campaign's images."""
    keys = set(doc.get("key") for doc in assets.values())
    for doc in campaigns.values():
        keys.update(image.get("key") for image in doc.get("images") or [] if isinstance(image, dict))
    keys.discard(None)
    return keys


def deletable(workspace, legacy_objects, present, named):
    """``(delete, keep)`` for legacy objects: deletable only when nothing names
    them and their copy in the new layout exists at the same size. ``keep`` says
    why each other one stays."""
    delete, keep = [], []
    for key, size in sorted(legacy_objects.items()):
        try:
            target = new_key(key, workspace)
        except MoveError as ex:
            keep.append((key, str(ex)))
            continue
        if key in named:
            keep.append((key, "an index row still names it"))
        elif target not in present:
            keep.append((key, "nothing was copied to %s" % target))
        elif present[target] != size:
            keep.append((key, "%s is %s bytes, not %s" % (target, present[target], size)))
        else:
            delete.append((key, target))
    return delete, keep


# ------------------------------------------------------------------ bytes ----
def checked_body(r2, key, move):
    """The object's bytes, refused unless they are the ones the index names."""
    endpoint, bucket, access, secret = r2
    body = r2_sigv4.get_object(endpoint, bucket, key, access_key=access, secret_key=secret)
    if len(body) != move["bytes"] or hashlib.sha256(body).hexdigest() != move["sha256"]:
        raise MoveError("%s does not hold the bytes its index row names" % key)
    return body


def copy_objects(r2, moves, present, log=print):
    """Copy each move whose target is not already there. Returns the count."""
    endpoint, bucket, access, secret = r2
    copied = 0
    for old, move in sorted(moves.items()):
        if present.get(move["key"]) == move["bytes"]:
            checked_body(r2, move["key"], move)
            log("present  %s" % move["key"])
            continue
        if old not in present:
            raise MoveError("%s is not in the bucket, so there is nothing to copy" % old)
        body = checked_body(r2, old, move)
        r2_sigv4.put_object(endpoint, bucket, move["key"], body, move["content_type"],
                            access_key=access, secret_key=secret)
        log("copied   %s -> %s" % (old, move["key"]))
        copied += 1
    return copied


def bucket(r2, prefix):
    endpoint, bucket_name, access, secret = r2
    return dict((obj["key"], obj["bytes"]) for obj in r2_sigv4.list_objects(
        endpoint, bucket_name, prefix=prefix, access_key=access, secret_key=secret))


# -------------------------------------------------------------- firestore ----
class Firestore:
    """Reads for one workspace and, for --delete-old, the others' indexes."""

    def __init__(self, creds, token, workspace):
        self.creds, self.token, self.workspace = creds, token, workspace

    def env(self, workspace=None):
        return dict(os.environ, GENAIE_WORKSPACE_ID=workspace or self.workspace)

    def read(self, collection, workspace=None):
        return decode(operator_config.list_documents(
            self.creds["project_id"], collection, self.token, opener=urllib.request.urlopen,
            sleep=time.sleep, env=self.env(workspace)))

    def other_workspaces(self):
        root = dict(os.environ)
        root.pop("GENAIE_WORKSPACE_ID", None)
        rows = operator_config.list_documents(self.creds["project_id"], "workspaces", self.token,
                                              opener=urllib.request.urlopen, sleep=time.sleep, env=root)
        return sorted(doc_id for doc_id, _ in rows if doc_id != self.workspace)


def store_for(creds, cache, workspace):
    import base64
    import console_campaigns
    import json
    env = dict(os.environ, GENAIE_FIRESTORE_DIR=cache, GENAIE_WORKSPACE_ID=workspace,
               GCP_SERVICE_ACCOUNT_B64=base64.b64encode(json.dumps(creds).encode()).decode())
    env.pop("GCP_SERVICE_ACCOUNT_FILE", None)
    return console_campaigns.Store(env)


def credentials(key):
    env = dict(os.environ)
    if key:
        env["GCP_SERVICE_ACCOUNT_FILE"] = key
    env.pop("GCP_SERVICE_ACCOUNT_B64", None)
    creds = operator_config.credentials(env)
    if creds is None:
        raise MoveError("no key: pass --key or set GCP_SERVICE_ACCOUNT_FILE")
    return creds


# ------------------------------------------------------------------- main ----
def describe(moves, asset_changes, campaign_changes):
    for old, move in sorted(moves.items()):
        print("MOVE    %s -> %s (%d bytes)" % (old, move["key"], move["bytes"]))
    for doc_id in sorted(asset_changes):
        print("REPOINT %s/%s" % (ad_assets.COLLECTION, doc_id))
    for slug, (_before, after) in sorted(campaign_changes.items()):
        print("REPOINT campaigns/%s.images (%d picture(s))" % (slug, len(after)))


def write(args, workspace, creds, cache, r2, firestore):
    store = store_for(creds, cache, workspace)
    leases = active_leases(firestore.read("campaign_asset_uploads"), time.time())
    if leases:
        raise MoveError("an upload holds a lease (%s); try again once it expires" % ", ".join(leases))
    moves, _, _ = plan(workspace, firestore.read(ad_assets.COLLECTION), firestore.read("campaigns"))
    present = bucket(r2, None)
    copied = copy_objects(r2, moves, present)
    store.begin()
    try:
        assets = decode(store.list(ad_assets.COLLECTION))
        campaigns = decode(store.list("campaigns"))
        uploads = decode(store.list("campaign_asset_uploads"))
        leases = active_leases(uploads, time.time())
        if leases:
            raise MoveError("an upload holds a lease (%s); try again once it expires" % ", ".join(leases))
        again, asset_changes, campaign_changes = plan(workspace, assets, campaigns)
        if again != moves:
            raise MoveError("the index changed while copying; run --write again")
        writes = commit_writes(store.base, workspace, asset_changes, campaign_changes)
        if writes:
            store.request(":commit", {"transaction": store.transaction, "writes": writes})
            store.transaction = None
    finally:
        store.rollback()
    print("%d object(s) copied, %d index row(s) and %d campaign(s) repointed"
          % (copied, len(asset_changes), len(campaign_changes)))
    return 0


def verify(workspace, r2, firestore):
    assets = firestore.read(ad_assets.COLLECTION)
    campaigns = firestore.read("campaigns")
    moves, asset_changes, campaign_changes = plan(workspace, assets, campaigns)
    problems = ["%s is still in the old layout" % old for old in sorted(moves)]
    present = bucket(r2, "workspaces/%s/" % workspace)
    deleted = set(slug for slug, doc in campaigns.items() if doc.get("deleted") is True)
    checked = 0
    for doc_id, doc in sorted(assets.items()):
        if doc.get("campaign") in deleted or doc_id in asset_changes:
            continue
        entry = ad_assets._entry(doc_id, doc)
        if present.get(entry["key"]) != entry["bytes"]:
            problems.append("assets/%s names %s, which is missing or the wrong size" % (doc_id, entry["key"]))
            continue
        try:
            checked_body(r2, entry["key"], entry)
            checked += 1
        except (MoveError, r2_sigv4.R2Error) as ex:
            problems.append(str(ex))
    for slug, doc in sorted(campaigns.items()):
        if slug in deleted or slug in campaign_changes:
            continue
        for image in doc.get("images") or []:
            if present.get(image.get("key")) != image.get("bytes"):
                problems.append("campaigns/%s names %s, which is missing or the wrong size" % (slug, image.get("key")))
    for problem in problems:
        print("DRIFT  %s" % problem)
    print("verify: %s (%d picture(s) checked byte for byte)" % ("FAILED" if problems else "clean", checked))
    return 1 if problems else 0


def delete_old(args, workspace, r2, firestore):
    named = named_keys(firestore.read(ad_assets.COLLECTION), firestore.read("campaigns"))
    uploads = firestore.read("campaign_asset_uploads")
    leases = active_leases(uploads, time.time())
    if leases:
        raise MoveError("an upload holds a lease (%s); try again once it expires" % ", ".join(leases))
    # ads/ predates workspaces and is shared, so another workspace's index
    # naming a key there keeps it too.
    for other in firestore.other_workspaces():
        named |= named_keys(firestore.read(ad_assets.COLLECTION, other), firestore.read("campaigns", other))
    legacy = {}
    for prefix in legacy_prefixes(workspace):
        legacy.update(bucket(r2, prefix))
    present = bucket(r2, "workspaces/%s/" % workspace)
    delete, keep = deletable(workspace, legacy, present, named)
    for key, why in keep:
        print("KEEP    %s (%s)" % (key, why))
    for key, target in delete:
        print("DELETE  %s (copy at %s)" % (key, target))
    if not args.yes:
        print("dry run: nothing deleted (add --yes)")
        return 0
    endpoint, bucket_name, access, secret = r2
    for key, target in delete:
        # The copy is read back byte for byte before its original goes.
        size = present[target]
        body = r2_sigv4.get_object(endpoint, bucket_name, target, access_key=access, secret_key=secret)
        digest = ad_assets.KEY_RE.match(target).group(1)
        if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
            raise MoveError("%s does not hold the bytes its name says; %s kept" % (target, key))
        r2_sigv4.delete_object(endpoint, bucket_name, key, access_key=access, secret_key=secret)
        print("deleted %s" % key)
    print("%d old object(s) deleted, %d kept" % (len(delete), len(keep)))
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="copy the objects, then repoint the index")
    mode.add_argument("--verify", action="store_true", help="check the index against the new layout")
    mode.add_argument("--delete-old", action="store_true", help="list, or with --yes delete, the old objects")
    parser.add_argument("--yes", action="store_true", help="with --delete-old, really delete")
    parser.add_argument("--key", help="service-account key file (else GCP_SERVICE_ACCOUNT_FILE)")
    parser.add_argument("--env-file", metavar="PATH",
                        help="credentials file (default: %s)" % r2_sigv4.DEFAULT_ENV_FILE)
    parser.add_argument("--no-env-file", action="store_true", help="read credentials only from the environment")
    parser.add_argument("--endpoint", help="R2 S3 endpoint (else R2_ACCOUNT_ID)")
    parser.add_argument("--bucket", help="R2 bucket (else R2_BUCKET)")
    args = parser.parse_args(argv[1:])
    if args.yes and not args.delete_old:
        parser.error("--yes only goes with --delete-old")

    try:
        if not args.no_env_file:
            loaded = r2_sigv4.load_env_file(args.env_file or r2_sigv4.DEFAULT_ENV_FILE,
                                            required=bool(args.env_file))
            if loaded:
                print("credentials from %s" % loaded)
        workspace = operator_config.workspace_id()
        if workspace is None:
            raise MoveError("set GENAIE_WORKSPACE_ID to the workspace whose pictures move")
        r2 = r2_sigv4.settings(endpoint=args.endpoint, bucket=args.bucket)
        print("workspace %s, R2 target %s" % (workspace, r2_sigv4.target(r2[0], r2[1])))
        creds = credentials(args.key)
        with tempfile.TemporaryDirectory() as cache:
            token = operator_config.access_token(
                creds, os.path.join(cache, "token.json"), opener=urllib.request.urlopen,
                signer=operator_config.node_signer, now=time.time, sleep=time.sleep)
            firestore = Firestore(creds, token, workspace)
            if args.verify:
                return verify(workspace, r2, firestore)
            if args.delete_old:
                return delete_old(args, workspace, r2, firestore)
            if args.write:
                return write(args, workspace, creds, cache, r2, firestore)
            assets = firestore.read(ad_assets.COLLECTION)
            campaigns = firestore.read("campaigns")
            moves, asset_changes, campaign_changes = plan(workspace, assets, campaigns)
            describe(moves, asset_changes, campaign_changes)
            leases = active_leases(firestore.read("campaign_asset_uploads"), time.time())
            if leases:
                print("LEASE   %s (--write waits for it to expire)" % ", ".join(leases))
            named = named_keys(assets, campaigns)
            legacy = {}
            for prefix in legacy_prefixes(workspace):
                legacy.update(bucket(r2, prefix))
            for key in sorted(set(legacy) - named):
                print("SPARE   %s (no index row names it; not moved)" % key)
            missing = sorted(old for old in moves if old not in legacy)
            for key in missing:
                print("MISSING %s (named, but not in the bucket)" % key)
            print("%d object(s) to move, %d index row(s) and %d campaign(s) to repoint; "
                  "dry run: nothing written (use --write)"
                  % (len(moves), len(asset_changes), len(campaign_changes)))
            return 1 if missing else 0
    except (MoveError, r2_sigv4.R2Error) as ex:
        sys.stderr.write("move-pictures: %s\n" % ex)
        return 2
    except (operator_config.ConfigUnavailable, operator_config.ConfigInvalid,
            ad_assets.AssetsUnavailable) as ex:
        sys.stderr.write("move-pictures: %s\n" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
