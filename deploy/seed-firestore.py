#!/usr/bin/env python3
"""Mirror workspace/cron/membership.json into Firestore, and prove the copy is exact.

Firestore becomes the canonical store for operator config (see
workspace/lib/operator_config.py). Until production is cut over to it,
membership.json is still canonical and this script keeps Firestore an exact
mirror of it. After cutover membership.json is deleted, so the script has nothing
to read and cannot overwrite edits made in the Firebase Console.

Three modes, and the default writes nothing:

    seed-firestore.py                    show what --write would change
    seed-firestore.py --write            apply it (add --prune to delete strays)
    seed-firestore.py --verify           read Firestore back, diff against the file

``--verify`` is the parity gate. It reads through the same operator_config code
production will use and fails unless the result is identical to membership.json:
every field, the order of groups, lines and campaigns, and the targets.json
generated for every campaign, byte for byte.

Credentials: a service-account key with Cloud Datastore User (writes) for
--write, or any key with read access for --verify, given by --key or
GCP_SERVICE_ACCOUNT_FILE. Use the seed key, not the production runtime key,
which is deliberately read-only. Keep the seed key off the server and delete it
when seeding is done.

Each campaign's ``enabled`` comes from deploy/promo-crons.json, read the way the
rest of the repo reads it: absent means on, and a non-boolean is an error.

Exit codes: 0 done / in parity, 1 drift found or a write failed, 2 bad input.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "workspace" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import operator_config  # noqa: E402
import operator_writer  # noqa: E402
import promo_membership  # noqa: E402

MEMBERSHIP = ROOT / "workspace" / "cron" / "membership.json"
MANIFEST = ROOT / "deploy" / "promo-crons.json"
# Shaping documents and writing them is shared with the console's write bridge,
# so the two cannot disagree about what a legal document looks like or how a
# change reaches Firestore.
plan_writes = operator_writer.plan_writes
RANK_STEP = operator_writer.RANK_STEP
COMMIT_BATCH = operator_writer.COMMIT_BATCH


class SeedError(Exception):
    pass


def documents(membership, switches):
    """The shared shaping, plus the precondition only the seed has: its two
    sources must declare the same campaigns. ``operator_writer.documents`` reads
    each slug's switch by name, and a slug in one source and not the other is a
    mistake in the repo, not something to paper over with a default."""
    declared = list(membership.get("promos") or {})
    if sorted(declared) != sorted(switches):
        raise SeedError("membership promos %s and promo-crons.json slugs %s differ"
                        % (sorted(declared), sorted(switches)))
    return operator_writer.documents(membership, switches)


def manifest_switches(manifest):
    """``{slug: bool}`` from the cron manifest, house semantics."""
    try:
        return operator_config.manifest_switches(manifest)
    except operator_config.ConfigInvalid as ex:
        raise SeedError(str(ex))


# The comparison lives beside the reader so the seed's --verify and production's
# shadow mode judge parity by exactly the same rules.
parity_diff = operator_config.parity_diff


def load_sources():
    try:
        membership = json.loads(MEMBERSHIP.read_text(encoding="utf-8"))
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except FileNotFoundError as ex:
        raise SeedError("%s is gone -- after cutover Firestore is canonical and "
                        "there is nothing to seed from" % ex.filename)
    except ValueError as ex:
        raise SeedError("source is not valid JSON: %s" % ex)
    errors = promo_membership.validate(membership)
    if errors:
        raise SeedError("membership.json does not validate; refusing to mirror it:\n  "
                        + "\n  ".join(errors))
    return membership, manifest_switches(manifest)


def _client(key):
    env = dict(os.environ)
    if key:
        env["GCP_SERVICE_ACCOUNT_FILE"] = key
    # Never pick up a runtime key from the environment by accident: the seed
    # names its key explicitly.
    env.pop("GCP_SERVICE_ACCOUNT_B64", None)
    creds = operator_config.credentials(env)
    if creds is None:
        raise SeedError("no key: pass --key or set GCP_SERVICE_ACCOUNT_FILE")
    return creds


def _token(creds, cache_dir):
    # The seed key's token goes in a throwaway cache, never the runtime one.
    return operator_config.access_token(
        creds, os.path.join(cache_dir, "token.json"), opener=urllib.request.urlopen,
        signer=operator_config.node_signer, now=time.time, sleep=time.sleep)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--key", help="service-account key file (else GCP_SERVICE_ACCOUNT_FILE)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="apply the changes")
    mode.add_argument("--verify", action="store_true", help="read back and diff")
    parser.add_argument("--prune", action="store_true",
                        help="with --write, delete Firestore documents the file lacks")
    args = parser.parse_args(argv[1:])
    if args.prune and not args.write:
        parser.error("--prune only makes sense with --write")

    try:
        membership, switches = load_sources()
        desired = documents(membership, switches)
        creds = _client(args.key)
        with tempfile.TemporaryDirectory() as cache:
            token = _token(creds, cache)
            raw = dict((c, operator_config.list_documents(
                creds["project_id"], c, token, opener=urllib.request.urlopen,
                sleep=time.sleep)) for c in operator_config.COLLECTIONS)
            if args.verify:
                got, got_switches, undecodable = operator_config.assemble(raw)
                problems = [p for found in undecodable.values() for p in found]
                problems += parity_diff(membership, switches, got, got_switches)
                for problem in problems:
                    print("DRIFT  %s" % problem)
                print("parity: %s (%d groups, %d lines, %d campaigns)" % (
                    "FAILED" if problems else "exact", len(got.get("groups") or {}),
                    len((got.get("defaults") or {}).get("accounts") or {}),
                    len(got.get("promos") or {})))
                return 1 if problems else 0

            existing = dict((c, [(doc_id, operator_config.decode_fields(fields))
                                 for doc_id, fields in rows])
                            for c, rows in raw.items())
            writes, strays, unchanged = plan_writes(desired, existing)
            for collection, doc_id, _, exists in writes:
                print("%s %s/%s" % ("REPLACE" if exists else "CREATE ", collection, doc_id))
            for collection, doc_id in strays:
                print("%s %s/%s" % ("DELETE " if args.prune else "STRAY  ", collection, doc_id))
            print("%d to write, %d unchanged, %d stray%s" % (
                len(writes), unchanged, len(strays),
                "" if args.prune or not strays else " (kept; --prune deletes them)"))
            if not args.write:
                print("dry run: nothing written (use --write)")
                return 0
            operator_writer.commit(creds["project_id"], token, writes,
                                   strays if args.prune else [])
            print("written to project %s" % creds["project_id"])
            return 0
    except SeedError as ex:
        sys.stderr.write("seed-firestore: %s\n" % ex)
        return 2
    except (operator_config.ConfigUnavailable, operator_config.ConfigInvalid) as ex:
        sys.stderr.write("seed-firestore: %s\n" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
