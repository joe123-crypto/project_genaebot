#!/usr/bin/env python3
"""Copy every operator document between the root collections and a workspace.

A bot deployment belongs to one workspace: with ``GENAIE_WORKSPACE_ID`` set it
reads and writes ``workspaces/<id>/<collection>/...`` instead of the root
collections. Moving an existing install into its workspace is this copy, run
before the variable is set:

    copy-workspace-data.py --from root --to workspace:<id>             what it would copy
    copy-workspace-data.py --from root --to workspace:<id> --write     copy it
    copy-workspace-data.py --from root --to workspace:<id> --verify    prove the copy is exact
    copy-workspace-data.py --from root --to workspace:<id> --delete-source --confirm <id>

It copies, never moves. The source is untouched until ``--delete-source``. That
refuses while any source document has no copy at the destination. Documents that
differ are expected by then: once the bot reads its workspace, the destination is
the newer copy. Running it the other way (``--from workspace:<id> --to root``)
is the rollback after a cutover.

Documents are copied as Firestore's raw typed values, not decoded and
re-encoded, so every type (``integerValue`` strings, timestamps, nulls) arrives
exactly as it left, and ``--verify`` is a plain comparison of those values.
Re-running ``--write`` replaces only documents that differ, so it is idempotent.

Nothing is left behind silently: the source's collection list is read from
Firestore, and a collection this script does not know about stops the run
rather than being skipped. Some root collections are known and deliberately
not operator data, so they are never copied or deleted:

* ``consoleUsers`` and ``workspaces`` -- the console's registry of who owns
  which workspace;
* ``users`` and ``pendingGmailTokens`` -- agent_genaie's, which shares this
  Firebase project (its user profiles, and its short-lived encrypted Gmail
  sign-up tokens).

Credentials: a key file given by --key or GCP_SERVICE_ACCOUNT_FILE (never the
base64 runtime variable). Reading needs Cloud Datastore Viewer; --write and
--delete-source need Cloud Datastore User.

Exit codes: 0 done / identical, 1 differences found or a write failed, 2 bad input.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "workspace" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

sys.path.insert(0, str(ROOT / "deploy"))

import ad_assets  # noqa: E402
import operator_config  # noqa: E402
import operator_writer  # noqa: E402
from workspace_tools import ToolError as CopyError, connect, label, parse_side, side_env  # noqa: E402

# The same set the backup takes: config, picture index, history, receipts.
COLLECTIONS = (operator_config.COLLECTIONS
               + (ad_assets.COLLECTION, operator_writer.AUDIT_COLLECTION,
                  "campaign_creations", "campaign_deletions", "campaign_asset_uploads",
                  "schedule_operations"))
# Root collections that are known and are not a workspace's data. The console's
# registry must never move into a workspace, and another app's data never
# belongs to one: agent_genaie shares this Firebase project.
REGISTRY = ("consoleUsers", "workspaces")
OTHER_APPS = ("users", "pendingGmailTokens")


def check_collections(store, env, wid):
    """Refuse a source holding a collection this script would not copy."""
    found = set(store.collection_ids(env))
    if wid is None:
        found -= set(REGISTRY) | set(OTHER_APPS)
    unknown = sorted(found - set(COLLECTIONS))
    if unknown:
        raise CopyError("%s holds collections this copy does not know: %s. If they are "
                        "operator data, add them to COLLECTIONS (and to the backup); if "
                        "another app sharing the project owns them, add them to "
                        "OTHER_APPS. Until then nothing guarantees they are handled."
                        % (label(wid), ", ".join(unknown)))


def compare(source, destination):
    """Per collection: what is missing, what differs, what is only on the
    destination. Raw values compared as canonical JSON."""
    canon = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False)  # noqa: E731
    report = {}
    for collection in COLLECTIONS:
        src, dst = source[collection], destination[collection]
        report[collection] = {
            "source": len(src), "destination": len(dst),
            "missing": sorted(set(src) - set(dst)),
            "differ": sorted(doc_id for doc_id in set(src) & set(dst)
                             if canon(src[doc_id]) != canon(dst[doc_id])),
            "extra": sorted(set(dst) - set(src)),
        }
    return report


def identical(report):
    return all(not (row["missing"] or row["differ"] or row["extra"]) for row in report.values())


def print_report(report, out, *, heading):
    out.write("%s\n" % heading)
    for collection in COLLECTIONS:
        row = report[collection]
        out.write("  %-24s %5d source  %5d destination  %d missing  %d differ  %d extra\n"
                  % (collection, row["source"], row["destination"], len(row["missing"]),
                     len(row["differ"]), len(row["extra"])))
        for kind in ("missing", "differ", "extra"):
            for doc_id in row[kind][:20]:
                out.write("      %-7s %s\n" % (kind, doc_id))
            if len(row[kind]) > 20:
                out.write("      ... and %d more %s\n" % (len(row[kind]) - 20, kind))


def read_side(store, env):
    return dict((collection, store.documents(env, collection)) for collection in COLLECTIONS)


def run(args, env, *, opener=None, signer=None, now=None, sleep=None, out=sys.stdout):
    src_wid, dst_wid = parse_side(args.source), parse_side(args.destination)
    if src_wid == dst_wid:
        raise CopyError("--from and --to are the same tree")
    if args.delete_source:
        involved = src_wid if src_wid is not None else dst_wid
        if args.confirm != involved:
            raise CopyError("--delete-source deletes %s; repeat the workspace id with "
                            "--confirm %s" % (label(src_wid), involved))
    with tempfile.TemporaryDirectory() as cache:
        store, base_env = connect(args.key, env, opener=opener, signer=signer, now=now,
                                  sleep=sleep, cache_dir=cache)
        src_env, dst_env = side_env(base_env, src_wid), side_env(base_env, dst_wid)
        check_collections(store, src_env, src_wid)
        source = read_side(store, src_env)
        destination = read_side(store, dst_env)
        report = compare(source, destination)
        heading = "%s -> %s (project %s)" % (label(src_wid), label(dst_wid), store.project)

        if args.write:
            operations = [{"update": {"name": store.name(dst_env, collection, doc_id),
                                      "fields": source[collection][doc_id]}}
                          for collection in COLLECTIONS
                          for doc_id in report[collection]["missing"] + report[collection]["differ"]]
            store.commit(operations)
            out.write("wrote %d documents to %s\n" % (len(operations), label(dst_wid)))
            destination = read_side(store, dst_env)
            report = compare(source, destination)
            heading += ", after writing"

        print_report(report, out, heading=heading)
        same = identical(report)
        if args.delete_source:
            missing = sum(len(row["missing"]) for row in report.values())
            if missing:
                out.write("not deleting: %d source documents have no copy at %s; check "
                          "them and delete them by hand, or copy them first\n"
                          % (missing, label(dst_wid)))
                return 1
            operations = [{"delete": store.name(src_env, collection, doc_id)}
                          for collection in COLLECTIONS for doc_id in source[collection]]
            store.commit(operations)
            out.write("deleted %d documents from %s\n" % (len(operations), label(src_wid)))
            return 0
        if args.verify or args.write:
            out.write("identical\n" if same else "DIFFERENT\n")
            return 0 if same else 1
        pending = sum(len(row["missing"]) + len(row["differ"]) for row in report.values())
        out.write("dry run: --write would copy %d documents\n" % pending)
        return 0


def main(argv, env=None, *, opener=None, signer=None, now=None, sleep=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--from", dest="source", required=True,
                        help="root or workspace:<id>")
    parser.add_argument("--to", dest="destination", required=True,
                        help="root or workspace:<id>")
    parser.add_argument("--key", help="service-account key file (else GCP_SERVICE_ACCOUNT_FILE)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="copy what is missing or differs")
    mode.add_argument("--verify", action="store_true", help="exit 1 unless the copy is exact")
    mode.add_argument("--delete-source", action="store_true",
                      help="delete the source documents, once every one has a copy")
    parser.add_argument("--confirm", help="the workspace id, required by --delete-source")
    args = parser.parse_args(argv)
    try:
        return run(args, os.environ if env is None else env, opener=opener, signer=signer,
                   now=now, sleep=sleep, out=out)
    except CopyError as ex:
        sys.stderr.write("copy-workspace-data: %s\n" % ex)
        return 2
    except (operator_config.ConfigUnavailable, operator_config.ConfigInvalid) as ex:
        sys.stderr.write("copy-workspace-data: %s\n" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
