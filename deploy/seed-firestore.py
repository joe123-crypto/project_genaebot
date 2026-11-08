#!/usr/bin/env python3
"""Mirror workspace/cron/membership.json into Firestore, and prove the copy is exact.

Firestore becomes the canonical store for operator config (see
workspace/lib/operator_config.py). Until production is cut over to it,
membership.json is still canonical and this script keeps Firestore an exact
mirror of it. After cutover membership.json is deleted, so the script has nothing
to read and cannot overwrite edits made in the Firebase Console.

Four modes, and the default writes nothing:

    seed-firestore.py                    show what --write would change
    seed-firestore.py --write            apply it (add --prune to delete strays)
    seed-firestore.py --verify           read Firestore back, diff against the file
    seed-firestore.py --export           the reverse: write Firestore into the file

``--export`` exists because the console now edits the store directly, so the file
falls behind with every edit -- and a rollback to file mode would silently undo
all of them, resurrecting deleted groups among others. It writes an exact mirror
or nothing (a store with a quarantined group is refused), keeps the file's own
key order so an unchanged store produces no diff, and names any campaign switch
it cannot carry: in file mode those live in promo-crons.json, not here.

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


# The order membership.json has always been written in. Firestore keeps no field
# order, so an export imposes this one: exporting a store that matches the file
# then reproduces the file byte for byte, and a real change is the only diff.
TOP_ORDER = ("_comment", "version", "defaults", "promos", "groups")
DEFAULTS_ORDER = ("activity", "caps_enabled", "contact_account", "accounts", "bands")
PROMO_ORDER = ("comment", "weekly_comment", "dir")
GROUP_ORDER = ("name", "source", "status", "activity", "account", "daily_cap",
               "promos", "paused_promos", "weekly", "note")


def _ordered(data, order, previous=None):
    """``data`` with keys in a stable order: the order ``previous`` (the same
    object in the file as committed) already had, then ``order``, then anything
    else alphabetically.

    The previous order wins because the file has been edited by hand for months
    and is not perfectly canonical -- a few groups list ``source`` or ``name``
    last. Imposing an order would reshuffle those on every export, so a review
    would have to wade through moves to find the real change."""
    keys = [key for key in (previous or {}) if key in data]
    keys += [key for key in order if key in data and key not in keys]
    keys += sorted(key for key in data if key not in keys)
    return dict((key, data[key]) for key in keys)


def canonical(membership, previous=None):
    """``membership`` in the file's key order. Groups, lines and campaigns keep
    the rank order the reader gives them, which is the file's own order."""
    previous = previous if isinstance(previous, dict) else {}

    def was(section):
        value = previous.get(section)
        return value if isinstance(value, dict) else {}

    out = _ordered(membership, TOP_ORDER, previous)
    if isinstance(out.get("defaults"), dict):
        out["defaults"] = _ordered(out["defaults"], DEFAULTS_ORDER, was("defaults"))
    for section, order in (("promos", PROMO_ORDER), ("groups", GROUP_ORDER)):
        if isinstance(out.get(section), dict):
            out[section] = dict(
                (key, _ordered(value, order, was(section).get(key))
                 if isinstance(value, dict) else value)
                for key, value in out[section].items())
    return out


def dumps_membership(membership, previous=None):
    """The exact text membership.json is committed as."""
    return json.dumps(canonical(membership, previous), indent=2,
                      ensure_ascii=False) + "\n"


def export_problems(membership, undecodable):
    """Why the store cannot be mirrored into the file, or ``[]``.

    An export is an exact mirror or nothing. A group the reader quarantines is one
    production does not advertise to; writing it into the file would produce a
    mirror that make-promo-targets refuses, which is worse than a stale one."""
    problems = [p for found in (undecodable or {}).values() for p in found]
    try:
        _, report = operator_config.quarantine(membership, undecodable)
    except operator_config.ConfigInvalid as ex:
        return problems + [str(ex)]
    problems += ["quarantined %s: %s" % (jid, "; ".join(found))
                 for jid, found in sorted(report["quarantined"].items())]
    if problems:
        return problems
    # The reader forgives some things the file does not: it drops a group's
    # reference to a deleted campaign, with only a warning. The export writes the
    # store as it is, so what decides is whether THAT text would load in file
    # mode -- a rollback file make-promo-targets refuses is no rollback.
    return ["the file would not validate: %s" % error
            for error in promo_membership.validate(membership)]


def switch_mismatches(store, manifest):
    """Campaign switches that cannot survive a rollback, as human sentences.

    In file mode a campaign's switch lives in promo-crons.json, not in
    membership.json, so an export cannot carry it. Any store switch that differs
    from the manifest is a change a rollback would silently undo."""
    out = []
    for slug in sorted(set(store) | set(manifest)):
        live, declared = store.get(slug, True), manifest.get(slug, True)
        if live != declared:
            out.append("%s is %s in Firestore but %s in promo-crons.json; rolling back "
                       "to file mode would switch it %s"
                       % (slug, "ON" if live else "OFF", "ON" if declared else "OFF",
                          "on" if declared else "off"))
    return out


def write_atomically(path, text):
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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


def export(key, *, opener=None, signer=None, now=None, sleep=None):
    """Mirror Firestore into membership.json. Exit 0 written (or already exact),
    1 the store cannot be mirrored, 2 bad input.

    Deliberately does NOT go through load_sources: that refuses to run once the
    file is gone, and an export is how the file is recreated."""
    opener = opener or urllib.request.urlopen
    signer = signer or operator_config.node_signer
    now = now or time.time
    sleep = sleep or time.sleep
    try:
        creds = _client(key)
        with tempfile.TemporaryDirectory() as cache:
            token = operator_config.access_token(
                creds, os.path.join(cache, "token.json"), opener=opener,
                signer=signer, now=now, sleep=sleep)
            raw = dict((c, operator_config.list_documents(
                creds["project_id"], c, token, opener=opener, sleep=sleep))
                for c in operator_config.COLLECTIONS)
        membership, store_switches, undecodable = operator_config.assemble(raw)
    except SeedError as ex:
        sys.stderr.write("seed-firestore: %s\n" % ex)
        return 2
    except (operator_config.ConfigUnavailable, operator_config.ConfigInvalid) as ex:
        sys.stderr.write("seed-firestore: %s\n" % ex)
        return 1

    problems = export_problems(membership, undecodable)
    if problems:
        for problem in problems:
            print("CANNOT EXPORT  %s" % problem)
        print("nothing written: the store does not validate, and a mirror of it "
              "would not either")
        return 1

    try:
        manifest = manifest_switches(json.loads(MANIFEST.read_text(encoding="utf-8")))
    except (OSError, ValueError, SeedError):
        manifest = {}
    for note in switch_mismatches(store_switches, manifest):
        print("NOTE  %s" % note)

    previous = MEMBERSHIP.read_text(encoding="utf-8") if MEMBERSHIP.exists() else None
    try:
        previous_doc = json.loads(previous) if previous is not None else None
    except ValueError:
        previous_doc = None
    text = dumps_membership(membership, previous_doc)
    if previous == text:
        print("exported: membership.json already matches Firestore (%d groups)"
              % len(membership.get("groups") or {}))
        return 0
    write_atomically(MEMBERSHIP, text)
    print("exported %d groups to %s%s" % (
        len(membership.get("groups") or {}), MEMBERSHIP,
        "" if previous is not None else " (created)"))
    print("then: ./workspace/bin/make-promo-targets --write, and commit both")
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--key", help="service-account key file (else GCP_SERVICE_ACCOUNT_FILE)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="apply the changes")
    mode.add_argument("--verify", action="store_true", help="read back and diff")
    mode.add_argument("--export", action="store_true",
                      help="write Firestore back into membership.json (the reverse "
                           "direction), so the file stays a truthful rollback")
    parser.add_argument("--prune", action="store_true",
                        help="with --write, delete Firestore documents the file lacks")
    args = parser.parse_args(argv[1:])
    if args.prune and not args.write:
        parser.error("--prune only makes sense with --write")

    if args.export:
        return export(args.key)

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
