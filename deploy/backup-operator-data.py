#!/usr/bin/env python3
"""Take a dated copy of every operator document, and put one back.

Git used to be the backup and the history for this data: the groups, the
campaigns, their copy and their pictures all sat in the repository, so any
mistake was one ``git revert`` away. Once they live only in Firestore and R2
that is gone, and the 48 hour last-known-good snapshot in ``runtime/firestore/``
is a cache for outages, not a backup -- it holds one generation and it is
overwritten by the next healthy read. This script is what replaces git.

Three modes, and the default is the backup:

    backup-operator-data.py                     write a dated backup, prune old ones
    backup-operator-data.py --verify            newest backup vs. a live read
    backup-operator-data.py --restore DIR --collection groups      what it would put back

A backup is a directory named for the day, holding one JSON file per collection
plus ``manifest.json`` with each file's document count and sha256. Documents are
decoded exactly as a campaign run decodes them
(``operator_config.decode_fields``), so a backup is readable by a person and can
be written straight back. The directory is assembled under a temporary name and
renamed into place, so a half-written backup is never mistaken for a whole one.

``audit`` is included and is the real change history -- who changed what, when,
and the before and after of every console write. The documents that only record
that an operation already happened (``campaign_creations``,
``campaign_deletions``, ``campaign_asset_uploads``) are included too, because
restoring the config without them would let a replayed request run a second
time.

R2 holds the picture bytes, which are far too large to copy here and are already
content-addressed, so what is recorded is the object list: every key, its size
and R2's own etag. That is enough to tell whether the bytes an index entry names
are still in the bucket. Listing needs bucket credentials, which the entrypoint
deliberately strips from the gateway environment, so a run on the gateway (the
weekly cron) records the Firestore side and says in the manifest that the bucket
was not listed. A run on the operator's machine, where
``~/.config/genaie/r2-upload.env`` is, records both.

``--restore`` writes whole documents, not the field patches
``workspace/bin/console-write`` is limited to, because a restore has to be able
to put back a field no console screen can edit. That is also why it is one
collection per run, dry by default, and prints every document it would change:
it is the blunt instrument kept for the day something is badly wrong, not a way
to edit config. Each restore commits one audit document of its own.

Credentials: any key with read access for a backup, given by --key or
GCP_SERVICE_ACCOUNT_FILE; the gateway's read-only runtime key is enough. A
restore needs Cloud Datastore User, so it needs the seed key.

Exit codes: 0 done / verified, 1 drift found or a write failed, 2 bad input.
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "workspace" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
sys.path.insert(0, str(ROOT / "deploy"))

import ad_assets  # noqa: E402
import campaign_deletion  # noqa: E402
import console_campaigns  # noqa: E402
import operator_config  # noqa: E402
import operator_writer  # noqa: E402
import r2_sigv4  # noqa: E402

# Everything a restore would need, in the order a person reads them. The config
# first, then the picture index, then the history, then the receipts that stop a
# replayed request running twice. The live console index is deliberately absent:
# it is a derived SQLite cache on the volume, rebuilt from these.
COLLECTIONS = (operator_config.COLLECTIONS
               + (ad_assets.COLLECTION, operator_writer.AUDIT_COLLECTION,
                  "campaign_creations", "campaign_deletions", "campaign_asset_uploads"))
MANIFEST_NAME = "manifest.json"
R2_NAME = "r2-objects.json"
VERSION = 1
DEFAULT_RETAIN = 8
# Backups go beside the other runtime state on the volume, not in the repository.
BACKUP_SUBDIR = os.path.join("runtime", "backups")
# A day is enough: the weekly cron writes one, and an operator taking a second
# one by hand on the same day means to replace it.
DAY_FORMAT = "%Y-%m-%d"


class BackupError(Exception):
    pass


# ------------------------------------------------------------------ store ----
def client(key, env=None):
    env = dict(os.environ if env is None else env)
    if key:
        env["GCP_SERVICE_ACCOUNT_FILE"] = key
    # Name the key or inherit one; never pick up the base64 runtime variable by
    # accident, exactly as the seed and the migration do.
    env.pop("GCP_SERVICE_ACCOUNT_B64", None)
    creds = operator_config.credentials(env)
    if creds is None:
        raise BackupError("no key: pass --key or set GCP_SERVICE_ACCOUNT_FILE")
    return creds, env


def read_collections(creds, cache_dir, *, opener=None, signer=None, now=None,
                     sleep=None, collections=COLLECTIONS):
    """``{collection: {doc_id: fields}}`` for every document, decoded.

    A collection that does not exist yet reads as empty, which is not an error:
    nothing has been deleted or created from the console in a fresh project."""
    opener = opener or urllib.request.urlopen
    signer = signer or operator_config.node_signer
    token = operator_config.access_token(
        creds, os.path.join(cache_dir, "token.json"), opener=opener,
        signer=signer, now=now or time.time, sleep=sleep or time.sleep)
    out = {}
    for collection in collections:
        rows = operator_config.list_documents(creds["project_id"], collection, token,
                                              opener=opener, sleep=sleep or time.sleep)
        out[collection] = dict((doc_id, operator_config.decode_fields(raw))
                               for doc_id, raw in rows)
    return out


def read_objects(env, *, endpoint=None, bucket=None, opener=None):
    """``(objects, note)``: the bucket's object list, or ``None`` and why not."""
    try:
        resolved_endpoint, resolved_bucket, access, secret = r2_sigv4.settings(
            env, endpoint=endpoint, bucket=bucket)
    except r2_sigv4.R2Error as ex:
        return None, str(ex)
    try:
        objects = r2_sigv4.list_objects(
            resolved_endpoint, resolved_bucket, access_key=access, secret_key=secret,
            opener=opener)
    except r2_sigv4.R2Error as ex:
        return None, str(ex)
    return {"bucket": r2_sigv4.target(resolved_endpoint, resolved_bucket),
            "objects": objects}, None


# ----------------------------------------------------------------- layout ----
def _dump(payload):
    """The bytes a backup file holds. Sorted keys and a trailing newline, so the
    same documents always produce the same digest."""
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")


def digest(body):
    return hashlib.sha256(body).hexdigest()


def _write(path, body):
    """0600 because a backup holds every group, number and caption the operator
    has; the directory is 0700 for the same reason, and neither relies on the
    other."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


def assemble(directory, documents, objects, note, *, project, at):
    """Write a whole backup into ``directory`` and return its manifest."""
    files = {}
    for collection, docs in sorted(documents.items()):
        body = _dump({"collection": collection, "count": len(docs), "documents": docs})
        _write(os.path.join(directory, "%s.json" % collection), body)
        files["%s.json" % collection] = {"documents": len(docs),
                                         "bytes": len(body), "sha256": digest(body)}
    r2 = {"listed": False, "reason": note}
    if objects is not None:
        body = _dump(objects)
        _write(os.path.join(directory, R2_NAME), body)
        files[R2_NAME] = {"documents": len(objects["objects"]),
                          "bytes": len(body), "sha256": digest(body)}
        r2 = {"listed": True, "bucket": objects["bucket"],
              "objects": len(objects["objects"])}
    manifest = {"version": VERSION, "createdAt": at, "project": project,
                "collections": list(sorted(documents)), "files": files, "r2": r2}
    _write(os.path.join(directory, MANIFEST_NAME), _dump(manifest))
    return manifest


def backups(root):
    """Existing backup directories, oldest first. Anything that is not a dated
    directory with a manifest is left alone and never pruned."""
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path) or not os.path.isfile(os.path.join(path, MANIFEST_NAME)):
            continue
        try:
            datetime.datetime.strptime(name, DAY_FORMAT)
        except ValueError:
            continue
        out.append(path)
    return out


def prune(root, retain, out=sys.stdout):
    """Delete all but the newest ``retain`` backups. Returns what it removed."""
    existing = backups(root)
    if retain <= 0 or len(existing) <= retain:
        return []
    removed = existing[:len(existing) - retain]
    for path in removed:
        shutil.rmtree(path)
        out.write("pruned %s\n" % os.path.basename(path))
    return removed


def read_manifest(directory):
    path = os.path.join(directory, MANIFEST_NAME)
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except FileNotFoundError:
        raise BackupError("%s is not a backup: no %s" % (directory, MANIFEST_NAME))
    except ValueError as ex:
        raise BackupError("%s is not valid JSON: %s" % (path, ex))
    if not isinstance(manifest, dict) or manifest.get("version") != VERSION:
        raise BackupError("%s was written by a different version of this script" % path)
    return manifest


def read_backup(directory, collection):
    path = os.path.join(directory, "%s.json" % collection)
    try:
        with open(path, "rb") as handle:
            body = handle.read()
    except FileNotFoundError:
        raise BackupError("%s holds no %s" % (directory, collection))
    manifest = read_manifest(directory)
    recorded = (manifest.get("files") or {}).get("%s.json" % collection, {})
    if recorded.get("sha256") and recorded["sha256"] != digest(body):
        raise BackupError("%s does not match its manifest digest; the backup is damaged"
                          % path)
    payload = json.loads(body.decode("utf-8"))
    documents = payload.get("documents")
    if not isinstance(documents, dict):
        raise BackupError("%s has no documents object" % path)
    return documents


# ------------------------------------------------------------------ modes ----
def backup(root, *, key, retain, env=None, endpoint=None, bucket=None, no_r2=False,
           now=None, out=sys.stdout, reader=None, r2_reader=None):
    creds, resolved_env = client(key, env)
    at = (now or (lambda: datetime.datetime.now(datetime.timezone.utc)))()
    with tempfile.TemporaryDirectory() as cache:
        documents = (reader or read_collections)(creds, cache)
    if no_r2:
        objects, note = None, "--no-r2"
    else:
        objects, note = (r2_reader or read_objects)(
            resolved_env, endpoint=endpoint, bucket=bucket)
    os.makedirs(root, mode=0o700, exist_ok=True)
    final = os.path.join(root, at.strftime(DAY_FORMAT))
    staging = tempfile.mkdtemp(prefix=".partial-", dir=root)
    try:
        os.chmod(staging, 0o700)
        manifest = assemble(staging, documents, objects, note,
                            project=creds["project_id"], at=at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        # Replacing today's backup is the point of a second run on the same day.
        if os.path.isdir(final):
            shutil.rmtree(final)
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    for collection in manifest["collections"]:
        out.write("  %-26s %d\n" % (collection, manifest["files"]["%s.json" % collection]["documents"]))
    if manifest["r2"]["listed"]:
        out.write("  %-26s %d in %s\n" % ("r2 objects", manifest["r2"]["objects"],
                                          manifest["r2"]["bucket"]))
    else:
        out.write("  %-26s not listed (%s)\n" % ("r2 objects", manifest["r2"]["reason"]))
    out.write("backed up to %s\n" % final)
    prune(root, retain, out=out)
    return manifest, final


def verify(root, *, key, directory=None, env=None, out=sys.stdout, reader=None):
    """Whether a backup still says what Firestore says. Reports drift per
    document, so the answer names what changed rather than just failing."""
    chosen = directory or (backups(root)[-1] if backups(root) else None)
    if chosen is None:
        raise BackupError("no backup in %s to verify" % root)
    creds, _ = client(key, env)
    with tempfile.TemporaryDirectory() as cache:
        live = (reader or read_collections)(creds, cache)
    problems = []
    for collection in read_manifest(chosen)["collections"]:
        saved = read_backup(chosen, collection)
        current = live.get(collection, {})
        for doc_id in sorted(set(saved) | set(current)):
            if doc_id not in current:
                problems.append("%s/%s is in the backup but not in Firestore" % (collection, doc_id))
            elif doc_id not in saved:
                problems.append("%s/%s is in Firestore but not in the backup" % (collection, doc_id))
            elif saved[doc_id] != current[doc_id]:
                changed = sorted(name for name in set(saved[doc_id]) | set(current[doc_id])
                                 if saved[doc_id].get(name) != current[doc_id].get(name))
                problems.append("%s/%s differs in %s" % (collection, doc_id, ", ".join(changed)))
    for problem in problems:
        out.write("DRIFT  %s\n" % problem)
    out.write("%s: %s\n" % (os.path.basename(chosen),
                            "matches Firestore" if not problems
                            else "%d document(s) differ" % len(problems)))
    return not problems


def _without_retired(document, retired):
    """A group document with every assignment to a retired campaign removed."""
    clean, stripped = dict(document), set()
    for field in ("promos", "paused_promos"):
        values = clean.get(field)
        if isinstance(values, list) and set(values) & retired:
            stripped |= set(values) & retired
            clean[field] = [slug for slug in values if slug not in retired]
    weekly = clean.get("weekly")
    if isinstance(weekly, dict) and set(weekly) & retired:
        stripped |= set(weekly) & retired
        clean["weekly"] = {slug: days for slug, days in weekly.items() if slug not in retired}
    return clean, stripped


def restore(directory, collection, *, key, write, actor, env=None, store=None,
            out=sys.stdout):
    """Put one collection back, whole documents and all. Dry by default."""
    if collection not in COLLECTIONS:
        raise BackupError("%s is not a backed-up collection (try: %s)"
                          % (collection, ", ".join(COLLECTIONS)))
    saved = read_backup(directory, collection)
    if store is None:
        _, resolved_env = client(key, env)
        store = console_campaigns.Store(resolved_env)
    if write:
        store.begin()
    try:
        # A retirement is deliberate and irreversible to every reader, so a
        # backup taken before it must not quietly undo it: the campaign document
        # is left as the marker, and a restored group loses its assignments to
        # it. This is the one thing a restore refuses to put back.
        retired = campaign_deletion.deleted_slugs({"campaigns": store.list("campaigns")})
        changes, held = [], []
        for doc_id in sorted(saved):
            after = saved[doc_id]
            if collection == "campaigns" and doc_id in retired:
                held.append(doc_id)
                continue
            if collection == "groups" and retired:
                after, stripped = _without_retired(after, retired)
                if stripped:
                    held.append("%s (%s)" % (doc_id, ", ".join(sorted(stripped))))
            before = store.get(collection, doc_id)
            if before != after:
                changes.append((doc_id, before, after))
        for doc_id, before, after in changes:
            fields = sorted(name for name in set(before or {}) | set(after)
                            if (before or {}).get(name) != after.get(name))
            out.write("  %-40s %-8s %s\n" % (doc_id, "create" if before is None else "update",
                                             ", ".join(fields)))
        extra = sorted(doc_id for doc_id, _ in store.list(collection) if doc_id not in saved)
        for doc_id in extra:
            # Never deleted here: a document created after the backup is far more
            # likely to be wanted than to be wreckage, and deleting it is a
            # separate, deliberate act through the console.
            out.write("  %-40s %-8s left alone\n" % (doc_id, "newer"))
        for entry in held:
            out.write("  %-40s %-8s retired; not put back\n" % (entry.split(" ")[0], "retired"))
        out.write("%s/%s: %d document(s) to put back, %d newer left alone, "
                  "%d held back by a retirement\n"
                  % (os.path.basename(directory), collection, len(changes), len(extra), len(held)))
        if not write:
            out.write("dry run: nothing written (use --write)\n")
            return True
        if not changes:
            return True
        audit_id, audit = operator_writer.audit_document(
            actor, collection, "*", {"restored": 0},
            {"restored": len(changes), "from": os.path.basename(directory)})
        store.commit([(collection, doc_id, after, None) for doc_id, _, after in changes]
                     + [(operator_writer.AUDIT_COLLECTION, audit_id, audit, False)])
        out.write("restored %d document(s) into %s\n" % (len(changes), collection))
        return True
    finally:
        store.rollback()


def default_root(env=None):
    """``runtime/backups`` on the volume -- beside ``runtime/firestore``, not
    inside it: that directory is the outage cache and is rewritten by every
    healthy read, which is exactly what a backup must not share a fate with."""
    env = os.environ if env is None else env
    root = env.get("OPENCLAW_STATE_DIR") or operator_config.DEFAULT_STATE_DIR
    return os.path.join(root, BACKUP_SUBDIR)


def main(argv, env=None, *, reader=None, r2_reader=None, store=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--key", help="service-account key file (else GCP_SERVICE_ACCOUNT_FILE)")
    parser.add_argument("--out", metavar="DIR", help="where backups live (default: %s)"
                        % os.path.join("<state>", BACKUP_SUBDIR))
    parser.add_argument("--retain", type=int, default=DEFAULT_RETAIN, metavar="N",
                        help="backups to keep, newest first (default %d; 0 keeps all)"
                             % DEFAULT_RETAIN)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", nargs="?", const=True, metavar="DIR",
                      help="compare a backup (default: the newest) with a live read")
    mode.add_argument("--restore", metavar="DIR", help="put one collection back from DIR")
    parser.add_argument("--collection", help="the collection --restore puts back")
    parser.add_argument("--write", action="store_true", help="apply a --restore")
    parser.add_argument("--actor", default=os.environ.get("USER") or "backup",
                        help="name recorded in the audit trail")
    parser.add_argument("--no-r2", action="store_true",
                        help="skip the bucket listing even where it is configured")
    parser.add_argument("--env-file", metavar="PATH",
                        help="R2 credentials file (default: %s)" % r2_sigv4.DEFAULT_ENV_FILE)
    parser.add_argument("--no-env-file", action="store_true", help="ignore that file")
    parser.add_argument("--endpoint", help="R2 S3 endpoint (else R2_ACCOUNT_ID)")
    parser.add_argument("--bucket", help="R2 bucket (else R2_BUCKET)")
    parser.add_argument("--json", action="store_true", help="print the manifest as JSON")
    args = parser.parse_args(argv[1:])
    if args.restore and not args.collection:
        parser.error("--restore needs --collection")
    if args.collection and not args.restore:
        parser.error("--collection only applies to --restore")
    if args.write and not args.restore:
        parser.error("--write only applies to --restore")

    environment = os.environ if env is None else env
    root = args.out or default_root(environment)
    try:
        if not args.no_env_file and not args.no_r2 and args.restore is None:
            r2_sigv4.load_env_file(args.env_file or r2_sigv4.DEFAULT_ENV_FILE,
                                   required=bool(args.env_file), env=environment)
        if args.verify:
            ok = verify(root, key=args.key, env=environment, reader=reader,
                        directory=None if args.verify is True else args.verify)
            return 0 if ok else 1
        if args.restore:
            restore(args.restore, args.collection, key=args.key, write=args.write,
                    actor=args.actor, env=environment, store=store)
            return 0
        manifest, _ = backup(root, key=args.key, retain=args.retain, env=environment,
                             endpoint=args.endpoint, bucket=args.bucket,
                             no_r2=args.no_r2, reader=reader, r2_reader=r2_reader)
        if args.json:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    except BackupError as ex:
        sys.stderr.write("backup-operator-data: %s\n" % ex)
        return 2
    except (operator_config.ConfigUnavailable, operator_config.ConfigInvalid,
            operator_writer.WriteRefused, r2_sigv4.R2Error) as ex:
        sys.stderr.write("backup-operator-data: %s\n" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
