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
import promo_membership  # noqa: E402

MEMBERSHIP = ROOT / "workspace" / "cron" / "membership.json"
MANIFEST = ROOT / "deploy" / "promo-crons.json"
# Rank in steps of ten, so a group added between two others in the Console
# needs no renumbering of its neighbours.
RANK_STEP = 10
# Firestore's limit on writes in one commit.
COMMIT_BATCH = 500


class SeedError(Exception):
    pass


def manifest_switches(manifest):
    """``{slug: bool}`` from the cron manifest, house semantics."""
    out = {}
    for row in manifest.get("promos") or []:
        value = row.get("enabled", True)
        if not isinstance(value, bool):
            raise SeedError("promo-crons.json: %s has non-boolean enabled=%r"
                            % (row.get("slug"), value))
        out[row["slug"]] = value
    return out


def documents(membership, switches):
    """``{collection: [(doc_id, fields)]}`` -- plain decoded fields, in file order,
    each carrying a ``rank`` so Firestore's by-ID listing can be put back in order."""
    declared = list((membership.get("promos") or {}))
    if sorted(declared) != sorted(switches):
        raise SeedError("membership promos %s and promo-crons.json slugs %s differ"
                        % (sorted(declared), sorted(switches)))
    settings = dict((name, membership[name])
                    for name in operator_config.TOP_LEVEL_SETTINGS if name in membership)
    settings.update((k, v) for k, v in (membership.get("defaults") or {}).items()
                    if k != "accounts")

    def ranked(items):
        return [(doc_id, dict(data, rank=index * RANK_STEP))
                for index, (doc_id, data) in enumerate(items)]

    return {
        "settings": [(operator_config.SETTINGS_DOC, settings)],
        "lines": ranked(((membership.get("defaults") or {}).get("accounts") or {}).items()),
        "campaigns": ranked((slug, dict(meta, enabled=switches[slug]))
                            for slug, meta in (membership.get("promos") or {}).items()),
        "groups": ranked((membership.get("groups") or {}).items()),
    }


def parity_diff(expected, expected_switches, got, got_switches):
    """Human-readable differences; empty means the copy is exact. Content is
    compared field by field, then order (which decides who a short run reaches),
    then the generated targets for every campaign -- the payload the send path
    actually consumes."""
    problems = []
    canon = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False)  # noqa: E731
    exp_docs = operator_config._by_document(expected, expected_switches)
    got_docs = operator_config._by_document(got, got_switches)
    for key in sorted(set(exp_docs) | set(got_docs)):
        if key not in got_docs:
            problems.append("%s/%s is missing from Firestore" % key)
        elif key not in exp_docs:
            problems.append("%s/%s is in Firestore but not in the file" % key)
        elif canon(exp_docs[key]) != canon(got_docs[key]):
            fields = sorted(name for name in set(exp_docs[key]) | set(got_docs[key])
                            if canon(exp_docs[key].get(name)) != canon(got_docs[key].get(name)))
            problems.append("%s/%s differs in %s" % (key + (", ".join(fields),)))
    for label, pick in (("group", lambda m: list(m.get("groups") or {})),
                        ("line", lambda m: list((m.get("defaults") or {}).get("accounts") or {})),
                        ("campaign", lambda m: list(m.get("promos") or {}))):
        if set(pick(expected)) == set(pick(got)) and pick(expected) != pick(got):
            problems.append("%s order differs" % label)
    if not problems:
        for promo in expected.get("promos") or {}:
            want = promo_membership.dumps_targets(promo_membership.targets_payload(expected, promo))
            have = promo_membership.dumps_targets(promo_membership.targets_payload(got, promo))
            if want != have:
                problems.append("generated targets for %s differ" % promo)
    return problems


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


def plan_writes(desired, existing):
    """``(writes, strays, unchanged)``: documents to create or replace, documents
    Firestore has that the file does not, and how many already match."""
    writes, strays, unchanged = [], [], 0
    for collection in operator_config.COLLECTIONS:
        have = dict(existing.get(collection) or [])
        want = dict(desired.get(collection) or [])
        for doc_id, fields in want.items():
            if have.get(doc_id) == fields:
                unchanged += 1
            else:
                writes.append((collection, doc_id, fields, doc_id in have))
        strays.extend((collection, doc_id) for doc_id in have if doc_id not in want)
    return writes, strays, unchanged


def commit(project, token, writes, deletes):
    """Apply full-document replaces and deletes, ``COMMIT_BATCH`` at a time.
    A replace without an update mask overwrites the whole document, which is what
    makes re-running the seed idempotent."""
    base = "projects/%s/databases/(default)/documents" % project
    operations = [{"update": {"name": "%s/%s/%s" % (base, collection, doc_id),
                              "fields": operator_config.encode_fields(fields)}}
                  for collection, doc_id, fields, _ in writes]
    operations += [{"delete": "%s/%s/%s" % (base, collection, doc_id)}
                   for collection, doc_id in deletes]
    url = "%s/%s:commit" % (operator_config.API_ROOT, base)
    for start in range(0, len(operations), COMMIT_BATCH):
        body = json.dumps({"writes": operations[start:start + COMMIT_BATCH]}).encode("utf-8")
        operator_config._request(url, opener=urllib.request.urlopen, sleep=time.sleep,
                                 data=body, method="POST",
                                 headers=dict(operator_config._auth(token),
                                              **{"Content-Type": "application/json"}))


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
            commit(creds["project_id"], token, writes, strays if args.prune else [])
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
