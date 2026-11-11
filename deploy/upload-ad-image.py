#!/usr/bin/env python3
"""Put an ad image in Cloudflare R2 and index it in Firestore, so swapping a
campaign's picture no longer needs a deploy.

The bot reads the index from Firestore and the bytes from R2 over plain public
HTTPS (see workspace/lib/ad_assets.py). This script is the only thing that
writes either, and it runs on the operator's machine: the R2 write credentials
never go into the container or into Railway's service variables.

Two modes, and the default writes nothing:

    upload-ad-image.py cv ~/new-cv-ad.png            show what it would do
    upload-ad-image.py cv ~/new-cv-ad.png --write    upload + index it
    upload-ad-image.py --verify                      index vs bucket

``--verify`` is the parity gate. It used to diff the index against a committed
mirror of it, deploy/ad-assets.json; both the mirror and the baked
``workspace/cron/*/assets`` the mirror was seeded from are gone, so there is
nothing in the repository left to compare. What matters now is the pair that is
actually load-bearing: every picture the index names must be an object in the
bucket, or a run fetches a 404 and posts nothing. So --verify lists the bucket
and reports an index entry with no object (which breaks a campaign) and an
object no entry names (which only wastes space).

Keys are content-addressed -- ``ads/<slug>/<sha256>.<ext>`` -- so an object is
immutable, no CDN or edge cache can serve stale bytes, and a cached file's name
proves its contents. Replacing a picture writes a new object and repoints the
index; it never overwrites one.

Every campaign is data-defined, so its own document carries the ``images`` a run
attaches. Indexing a picture therefore updates both the index and that list, in
one transaction: writing only the index would upload a swap that never goes out.

Credentials:
  Firestore  a key with Cloud Datastore User, via --key or GCP_SERVICE_ACCOUNT_FILE.
             Use the seed key, not the production runtime key, which is read-only.
  R2         R2_ACCOUNT_ID (or --endpoint), R2_BUCKET, R2_ACCESS_KEY_ID,
             R2_SECRET_ACCESS_KEY. An S3 API token scoped to the one bucket is
             enough; --verify needs list as well as object write.

Both are read from ~/.config/genaie/r2-upload.env when it exists (override with
--env-file, skip with --no-env-file); anything already in the environment wins.
That path is deliberately OUTSIDE the checkout. The repository is default-deny
except for `deploy/**`, which is un-ignored wholesale, so a file like
`deploy/r2.env` would be staged by `git add -A` -- and `deploy/ci/check.py` only
refuses tracked names beginning with `.env`, so it would not catch that one. The
file must also not be readable by anyone else, which this refuses to load.

Exit codes: 0 done / in parity, 1 drift found or a write failed, 2 bad input.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "workspace" / "lib"
for extra in (str(LIB), str(ROOT / "deploy")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import ad_assets  # noqa: E402
import console_campaigns  # noqa: E402
import operator_config  # noqa: E402
import operator_writer  # noqa: E402
import r2_sigv4  # noqa: E402

RANK_STEP = operator_writer.RANK_STEP
# Every key this script writes lives under here, so it is also what --verify
# lists: an object outside it is not an ad picture and is none of our business.
KEY_PREFIX = "ads/"
# Magic bytes, because content_type decides how the gateway treats the file and
# a mislabelled image is refused at send time, long after the upload.
MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))


class UploadError(Exception):
    pass


# -------------------------------------------------------------------- read ----
# What the campaigns are, and what each one's pictures are, both come from
# Firestore now. The two readers that used to be here -- the cron manifest for
# the campaign list and deploy/ad-assets.json for the index -- are gone with
# those files.


def allocate_id(slug, taken):
    """The lowest free ``<slug>-<n>``.

    Ids are allocated independently of ``rank`` on purpose. Deriving one from
    the other (``rank // RANK_STEP``) made two different rotation slots share a
    document -- rank 10 and rank 15 both gave ``cv-1`` -- so one silently
    overwrote the other; and it meant re-ranking a picture renamed its document,
    orphaning the old one. A rank is a position in the rotation and an id is an
    identity; keeping them apart lets either change without disturbing the
    other."""
    for number in range(1, len(taken) + 2):
        candidate = "%s-%d" % (slug, number)
        if candidate not in taken:
            return candidate
    raise UploadError("could not allocate a document id for %s" % slug)


def validate_campaign(slug, entries):
    """Refuse an index this tool should never produce, before anything is written.

    Firestore writes are keyed by document id, so two entries sharing one would
    collapse into a single write and lose a picture. Duplicate ranks and
    filenames are refused for the same reason: the rotation order keys on rank,
    and a campaign's own ``images`` list keys on filename, so two entries
    sharing either make the rotation a coin toss."""
    for field in ("id", "rank", "filename"):
        seen = {}
        for entry in entries:
            value = entry.get(field)
            if value in seen:
                raise UploadError("%s would have two images with %s %r: %s and %s"
                                  % (slug, field, value, seen[value],
                                     entry["filename"]))
            seen[value] = entry["filename"]


def describe(slug, path, rank, doc_id):
    """One index entry for the file at ``path``, digest and all."""
    try:
        body = Path(path).read_bytes()
    except OSError as ex:
        raise UploadError("cannot read %s: %s" % (path, ex))
    if not body:
        raise UploadError("%s is empty" % path)
    if len(body) > ad_assets.MAX_BYTES:
        raise UploadError("%s is %d bytes, over the %d byte limit the gateway "
                          "enforces on an image" % (path, len(body), ad_assets.MAX_BYTES))
    content_type = next((kind for magic, kind in MAGIC if body.startswith(magic)), None)
    if content_type is None:
        raise UploadError("%s is not a JPEG or a PNG (its first bytes are %r)"
                          % (path, body[:8]))
    digest = hashlib.sha256(body).hexdigest()
    filename = os.path.basename(path)
    extension = ad_assets.CONTENT_TYPES[content_type]
    if not ad_assets.FILENAME_RE.match(filename):
        raise UploadError("%s is not a plain image name (letters, digits, dot, "
                          "dash, underscore)" % filename)
    entry = {"id": doc_id,
             "campaign": slug, "filename": filename,
             "key": "ads/%s/%s%s" % (slug, digest, extension),
             "sha256": digest, "bytes": len(body), "content_type": content_type,
             "rank": rank, "enabled": True, "comment": None}
    # Validate through exactly the code production validates with, so nothing
    # can be uploaded that the bot would then quarantine.
    ad_assets._entry(entry["id"], entry)
    return entry, body


def with_files(campaigns, slug, paths, rank=None, replace=None, disable=None,
               comment=None):
    """``campaigns`` with ``paths`` added to ``slug``, as a new campaigns map.

    Takes and returns the same shape -- the ``{slug: [entry]}`` map that
    ``ad_assets._assemble`` hands back -- so calls compose. The argument is
    never mutated."""
    campaigns = dict((k, [dict(e) for e in v]) for k, v in campaigns.items())
    entries = campaigns.get(slug, [])
    for name in disable or []:
        found = [e for e in entries if e.get("filename") == name]
        if not found:
            raise UploadError("%s has no indexed image called %s" % (slug, name))
        for entry in found:
            entry["enabled"] = False
    taken = set(e["id"] for e in entries)
    for position, path in enumerate(paths):
        filename = os.path.basename(path)
        existing = next((e for e in entries if e.get("filename") == filename), None)
        if replace:
            target = next((e for e in entries if e.get("filename") == replace), None)
            if target is None:
                raise UploadError("%s has no indexed image called %s" % (slug, replace))
            doc_id, slot = target["id"], target.get("rank") or RANK_STEP
            entries = [e for e in entries if e is not target]
        elif existing is not None:
            # A new version of a picture already in the rotation. Keep its
            # identity and its slot: giving it a fresh id would leave the old
            # document in Firestore, still enabled, and production would rotate
            # the obsolete artwork alongside the replacement until someone
            # noticed --verify reporting drift.
            doc_id, slot = existing["id"], existing.get("rank") or RANK_STEP
            entries = [e for e in entries if e is not existing]
        else:
            doc_id = allocate_id(slug, taken)
            slot = max([e.get("rank") or 0 for e in entries] or [0]) + RANK_STEP
        if rank is not None:
            slot = rank + position * RANK_STEP
        taken.add(doc_id)
        entry, _body = describe(slug, path, slot, doc_id)
        if comment:
            entry["comment"] = comment
        entries = [e for e in entries if e["filename"] != entry["filename"]] + [entry]
    entries = sorted(entries, key=lambda e: (e.get("rank") or 0, e["filename"]))
    validate_campaign(slug, entries)
    campaigns[slug] = entries
    return campaigns


def bodies_for(campaigns, paths):
    """``{key: body}`` for every entry this run has the bytes of.

    Only the files named on the command line: an entry already in the index has
    its object in the bucket, which is what ``--verify`` is there to confirm.
    There used to be a second source here -- each campaign's committed
    ``assets/`` directory -- and seeding from it is what first filled the bucket.
    """
    found = {}
    for path in paths:
        body = Path(path).read_bytes()
        found[hashlib.sha256(body).hexdigest()] = body
    return dict((e["key"], found[e["sha256"]])
                for entries in campaigns.values() for e in entries
                if e["sha256"] in found)


def bucket_keys(endpoint, bucket, *, access_key, secret_key, opener=None):
    """``{key: size}`` for every ad object in the bucket."""
    return dict((obj["key"], obj["bytes"]) for obj in r2_sigv4.list_objects(
        endpoint, bucket, prefix=KEY_PREFIX, access_key=access_key,
        secret_key=secret_key, opener=opener))


# ------------------------------------------------------------------- write ----
# Reading the bucket credentials, resolving the endpoint and shortening the
# account id for the log all live in deploy/r2_sigv4.py, beside the signing they
# are for, so this script and deploy/backup-operator-data.py cannot disagree
# about where a credential comes from or refuse a world-readable file
# differently. They raise R2Error, which main() already handles beside
# UploadError.


def firestore_credentials(key):
    env = dict(os.environ)
    if key:
        env["GCP_SERVICE_ACCOUNT_FILE"] = key
    # Never pick up a runtime key by accident: this script names its key.
    env.pop("GCP_SERVICE_ACCOUNT_B64", None)
    creds = operator_config.credentials(env)
    if creds is None:
        raise UploadError("no key: pass --key or set GCP_SERVICE_ACCOUNT_FILE")
    return creds


def token_for(creds, cache_dir):
    return operator_config.access_token(
        creds, os.path.join(cache_dir, "token.json"), opener=urllib.request.urlopen,
        signer=operator_config.node_signer, now=time.time, sleep=time.sleep)


def fields_of(entry):
    """The document as Firestore holds it: the entry minus its own id."""
    return dict((k, v) for k, v in entry.items() if k != "id" and v is not None)


def plan(campaigns, existing):
    """``(writes, strays)``: full-document replaces, and indexed documents the
    new index no longer mentions."""
    want = dict((e["id"], fields_of(e)) for entries in campaigns.values()
                for e in entries)
    writes = [(ad_assets.COLLECTION, doc_id, want[doc_id], doc_id in existing)
              for doc_id in sorted(want) if existing.get(doc_id) != want[doc_id]]
    strays = [(ad_assets.COLLECTION, doc_id) for doc_id in sorted(existing)
              if doc_id not in want]
    return writes, strays


def rotations(campaigns, data_defined):
    """``{slug: images}`` for every campaign whose own ``images`` list no longer
    matches the index being written.

    A run attaches the pictures on the campaign's own document, not the ones in
    the index, so indexing a new picture has to update both or the swap would be
    uploaded, indexed and never sent."""
    out = {}
    for slug, entries in campaigns.items():
        if slug not in data_defined:
            continue
        # The index's own ordering, so this is the rotation a run would have
        # resolved from it.
        ordered = operator_config._ranked([(entry["id"], entry) for entry in entries])
        images = console_campaigns.images_from_assets([entry for _, entry in ordered])
        if images != data_defined[slug].get("images"):
            out[slug] = images
    return out


def bucket_diff(indexed, present):
    """Lines naming every way the index and the bucket disagree.

    An index entry with no object is the one that breaks a campaign: the run
    fetches its bytes before it posts anything, so a missing object costs that
    campaign its whole day. An object nothing names is only wasted space, and is
    expected for a short while after a picture is replaced, so it is reported
    separately and does not fail the gate on its own."""
    problems, orphans = [], []
    named = {}
    for slug in sorted(indexed):
        for entry in indexed[slug]:
            named[entry["key"]] = entry
            if entry["key"] not in present:
                problems.append("%s names %s, which is not in the bucket"
                                % (entry["id"], entry["key"]))
            elif present[entry["key"]] != entry["bytes"]:
                problems.append("%s says %s is %d bytes; the bucket has %d"
                                % (entry["id"], entry["key"], entry["bytes"],
                                   present[entry["key"]]))
    for key in sorted(present):
        if key not in named:
            orphans.append("%s is in the bucket and no index entry names it" % key)
    return problems, orphans


# -------------------------------------------------------------------- main ----
def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("campaign", nargs="?", help="campaign slug, e.g. cv")
    parser.add_argument("files", nargs="*", help="image files to upload")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="apply the changes")
    mode.add_argument("--verify", action="store_true",
                      help="check every indexed picture against the bucket")
    parser.add_argument("--prune", action="store_true",
                        help="with --write, delete index documents this change "
                             "drops")
    parser.add_argument("--rank", type=int, help="rotation slot for the first file")
    parser.add_argument("--replace", metavar="FILENAME",
                        help="take over this indexed image's rotation slot")
    parser.add_argument("--disable", metavar="FILENAME", action="append",
                        help="stop rotating this indexed image (repeatable)")
    parser.add_argument("--comment", help="what the new image shows")
    parser.add_argument("--key", help="service-account key file "
                                      "(else GCP_SERVICE_ACCOUNT_FILE)")
    parser.add_argument("--env-file", metavar="PATH",
                        help="credentials file (default: %s)" % r2_sigv4.DEFAULT_ENV_FILE)
    parser.add_argument("--no-env-file", action="store_true",
                        help="read credentials only from the environment")
    parser.add_argument("--endpoint", help="R2 S3 endpoint (else R2_ACCOUNT_ID)")
    parser.add_argument("--bucket", help="R2 bucket (else R2_BUCKET)")
    args = parser.parse_args(argv[1:])

    if args.prune and not args.write:
        parser.error("--prune only makes sense with --write")
    if args.verify and (args.campaign or args.files):
        parser.error("--verify checks the whole index; name no campaign")
    if not args.verify and not args.campaign:
        parser.error("name a campaign and its files, or pass --verify")

    try:
        if not args.no_env_file:
            loaded = r2_sigv4.load_env_file(args.env_file or r2_sigv4.DEFAULT_ENV_FILE,
                                            required=bool(args.env_file))
            if loaded:
                print("credentials from %s" % loaded)
        creds = firestore_credentials(args.key)
        with tempfile.TemporaryDirectory() as cache:
            token = token_for(creds, cache)
            rows = operator_config.list_documents(
                creds["project_id"], ad_assets.COLLECTION, token,
                opener=urllib.request.urlopen, sleep=time.sleep)
            campaigns_raw = operator_config.list_documents(
                creds["project_id"], "campaigns", token,
                opener=urllib.request.urlopen, sleep=time.sleep)
            everything = {slug: operator_config.decode_fields(fields)
                          for slug, fields in campaigns_raw}
            deleted = {slug for slug, doc in everything.items() if doc.get("deleted") is True}
            if args.campaign in deleted:
                raise UploadError("this campaign has been deleted")
            data_defined = {slug: doc for slug, doc in everything.items()
                            if doc.get("kind") == "console" and slug not in deleted}
            # A retired campaign's pictures belong to the deletion's own cleanup,
            # which removes them on its own schedule. Never plan around them.
            rows = [(doc_id, fields) for doc_id, fields in rows
                    if operator_config.decode_fields(fields).get("campaign") not in deleted]
            indexed, quarantined = ad_assets._assemble(rows)

            if args.verify:
                problems = ["%s is not a usable index document: %s" % (doc_id, why)
                            for doc_id, why in sorted(quarantined.items())]
                endpoint, bucket, access, secret = r2_sigv4.settings(
                    endpoint=args.endpoint, bucket=args.bucket)
                print("R2 target: %s" % r2_sigv4.target(endpoint, bucket))
                present = bucket_keys(endpoint, bucket, access_key=access,
                                      secret_key=secret)
                missing, orphans = bucket_diff(indexed, present)
                problems += missing
                for problem in problems:
                    print("DRIFT  %s" % problem)
                for orphan in orphans:
                    print("SPARE  %s" % orphan)
                print("parity: %s (%d campaigns, %d images, %d object(s), %d spare)"
                      % ("FAILED" if problems else "exact", len(indexed),
                         sum(len(v) for v in indexed.values()), len(present),
                         len(orphans)))
                return 1 if problems else 0

            if args.campaign not in data_defined:
                raise UploadError("%r is not a campaign in the store (try: %s)"
                                  % (args.campaign, ", ".join(sorted(data_defined))))
            if not args.files and not args.disable:
                raise UploadError("name at least one image file, or use --disable")
            campaigns = with_files(indexed, args.campaign, args.files,
                                   rank=args.rank, replace=args.replace,
                                   disable=args.disable, comment=args.comment)
            for slug, entries in sorted(campaigns.items()):
                validate_campaign(slug, entries)

            existing = dict((e["id"], fields_of(e))
                            for entries in indexed.values() for e in entries)
            writes, strays = plan(campaigns, existing)
            bodies = bodies_for(campaigns, args.files)
            uploads = [(e["key"], e["filename"], e["bytes"], e["content_type"])
                       for entries in sorted(campaigns.items())
                       for e in entries[1] if e["key"] in bodies]
            for key, name, size, _kind in uploads:
                print("UPLOAD  %s  (%s, %d bytes)" % (key, name, size))
            for _collection, doc_id, _fields, exists in writes:
                print("%s %s/%s" % ("REPLACE" if exists else "CREATE ",
                                    ad_assets.COLLECTION, doc_id))
            for _collection, doc_id in strays:
                print("%s %s/%s" % ("DELETE " if args.prune else "STRAY  ",
                                    ad_assets.COLLECTION, doc_id))
            needed = rotations(campaigns, data_defined)
            for slug in sorted(needed):
                print("UPDATE  campaigns/%s.images (%d picture(s))"
                      % (slug, len(needed[slug])))
            print("%d object(s) to upload, %d document(s) to write, %d stray%s"
                  % (len(uploads), len(writes), len(strays),
                     "" if args.prune or not strays else " (kept; --prune deletes them)"))

            # Check R2 on the dry run too. Resolving it only at write time means
            # a mistyped token is first discovered with some documents already
            # written, which is the one state this is awkward to recover from.
            r2 = None
            if uploads:
                try:
                    r2 = r2_sigv4.settings(endpoint=args.endpoint, bucket=args.bucket)
                except r2_sigv4.R2Error as ex:
                    if args.write:
                        raise
                    print("R2 not ready: %s" % ex)
                if r2:
                    print("R2 target: %s" % r2_sigv4.target(r2[0], r2[1]))
            if not args.write:
                print("dry run: nothing uploaded or written (use --write)")
                return 0

            # Reserve upload keys before the first PUT. Deletion reads these
            # receipts atomically and waits out their bounded upload lease, even
            # if the uploader crashes before indexing its objects.
            import console_campaigns
            import secrets
            store_env = dict(os.environ, GENAIE_FIRESTORE_DIR=cache, GCP_SERVICE_ACCOUNT_B64=__import__("base64").b64encode(json.dumps(creds).encode()).decode())
            store_env.pop("GCP_SERVICE_ACCOUNT_FILE", None)
            store = console_campaigns.Store(store_env)
            receipt = secrets.token_hex(16)
            planned = [dict(entry) for entries in campaigns.values() for entry in entries]
            store.begin()
            try:
                current = {slug: operator_config.decode_fields(fields) for slug, fields in store.list("campaigns")}
                if any(current.get(slug, {}).get("deleted") for slug in campaigns):
                    raise UploadError("campaign was deleted while preparing uploads; refresh the plan")
                store.commit([("campaign_asset_uploads", receipt, {"assets": planned,
                              "expiresAt": int(time.time()) + len(uploads) * r2_sigv4.HTTP_TIMEOUT_SECONDS + 600}, False)])
            finally:
                store.rollback()
            endpoint, bucket, access, secret = r2 if r2 else (None, None, None, None)
            for key, name, _size, content_type in uploads:
                r2_sigv4.put_object(endpoint, bucket, key, bodies[key], content_type,
                                    access_key=access, secret_key=secret)
                print("uploaded %s" % key)
            store.begin()
            try:
                current = {slug: operator_config.decode_fields(fields) for slug, fields in store.list("campaigns")}
                if any(current.get(slug, {}).get("deleted") for slug in campaigns):
                    raise UploadError("campaign was deleted during upload; its reserved pictures will be cleaned up")
                # The old plan must still describe the current index.
                current_assets = dict(store.list("assets"))
                if any(current_assets.get(key) != dict(rows).get(key) for key in [row[1] for row in writes] + ([row[1] for row in strays] if args.prune else [])):
                    raise UploadError("asset index changed during upload; refresh the plan")
                operations = [{"update": {"name": "%s/%s/%s" % (store.base, collection, key),
                                          "fields": operator_config.encode_fields(fields)},
                               "currentDocument": {"exists": exists}}
                              for collection, key, fields, exists in writes]
                operations += [{"delete": "%s/%s/%s" % (store.base, collection, key)} for collection, key in (strays if args.prune else [])]
                for slug, images in needed.items():
                    if current.get(slug, {}).get("kind") != "console":
                        raise UploadError("campaign conversion changed during upload; refresh the plan")
                    operations.append({"update": {"name": "%s/campaigns/%s" % (store.base, slug),
                                                  "fields": operator_config.encode_fields({"images": images})},
                                       "updateMask": {"fieldPaths": ["images"]},
                                       "currentDocument": {"exists": True}})
                operations.append({"delete": "%s/campaign_asset_uploads/%s" % (store.base, receipt)})
                if len(operations) > 500:
                    raise UploadError("asset update exceeds the atomic write limit; upload fewer files")
                store.request(":commit", {"transaction": store.transaction, "writes": operations})
                store.transaction = None
            finally:
                store.rollback()
            print("indexed in project %s; nothing to commit -- the index and "
                  "the bytes both live outside this repository now"
                  % creds["project_id"])
            return 0
    except (UploadError, r2_sigv4.R2Error) as ex:
        sys.stderr.write("upload-ad-image: %s\n" % ex)
        return 2
    except (operator_config.ConfigUnavailable, operator_config.ConfigInvalid,
            ad_assets.AssetsUnavailable) as ex:
        sys.stderr.write("upload-ad-image: %s\n" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
