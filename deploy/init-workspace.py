#!/usr/bin/env python3
"""Seed a new workspace with the two things a bot cannot send without.

A workspace starts empty. Its bot then has no ``settings/defaults`` (so every
read is invalid and targeting fails closed) and no ``lines`` (which the console
writer refuses to create, since a line is a WhatsApp account the operator links,
not a field to type in). This writes both:

    init-workspace.py --workspace <id> --line default=+263771234567              what it would write
    init-workspace.py --workspace <id> --line default=+263771234567 --write      write it

``settings/defaults`` is copied from an existing tree (``--settings-from``,
default ``root``), so a new workspace starts with the cap bands and tiers the
running install uses rather than numbers invented here. Its ``contact_account``
is pointed at the first ``--line`` (or ``--contact``), because the copied one
names a line this workspace does not have.

The result is validated with the bot's own validator before anything is
written, and an existing settings or line document is never overwritten.

Credentials: --key or GCP_SERVICE_ACCOUNT_FILE with Cloud Datastore User.
Exit codes: 0 done, 1 a read or write failed, 2 bad input.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "workspace" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
sys.path.insert(0, str(ROOT / "deploy"))

import operator_config  # noqa: E402
import operator_writer  # noqa: E402
from workspace_tools import ToolError, connect, label, parse_side, side_env  # noqa: E402

LINE_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,31})=(\+[1-9][0-9]{6,14})$")


def parse_lines(values):
    lines = []
    for value in values:
        matched = LINE_RE.match(value)
        if not matched:
            raise ToolError("--line %r is not <id>=+<international number>" % value)
        if matched.group(1) in dict(lines):
            raise ToolError("line %s is given twice" % matched.group(1))
        lines.append((matched.group(1), matched.group(2)))
    if not lines:
        raise ToolError("give at least one --line")
    return lines


def plan(settings, lines, contact):
    """The raw documents to write, after the bot's validator has accepted them."""
    settings = dict(settings)
    settings["contact_account"] = contact
    docs = [("settings", operator_config.SETTINGS_DOC, settings)]
    for index, (line_id, number) in enumerate(lines):
        docs.append(("lines", line_id, {"number": number,
                                        operator_config.RANK: (index + 1) * operator_writer.RANK_STEP}))
    raw = {"settings": [], "lines": [], "campaigns": [], "groups": []}
    for collection, doc_id, fields in docs:
        raw[collection].append((doc_id, operator_config.encode_fields(fields)))
    membership, _, undecodable = operator_config.assemble(raw)
    try:
        operator_config.quarantine(membership, undecodable)
    except operator_config.ConfigInvalid as ex:
        raise ToolError("the seeded config would be invalid: %s" % ex)
    return docs


def run(args, env, *, opener=None, signer=None, now=None, sleep=None, out=sys.stdout):
    wid = parse_side("workspace:%s" % args.workspace)
    source = parse_side(args.settings_from)
    if source == wid:
        raise ToolError("--settings-from is the workspace being seeded")
    lines = parse_lines(args.line)
    contact = args.contact or lines[0][0]
    if contact not in dict(lines):
        raise ToolError("--contact %s is not one of the --line ids" % contact)
    with tempfile.TemporaryDirectory() as cache:
        store, base_env = connect(args.key, env, opener=opener, signer=signer, now=now,
                                  sleep=sleep, cache_dir=cache)
        source_env, target_env = side_env(base_env, source), side_env(base_env, wid)
        settings = store.documents(source_env, "settings").get(operator_config.SETTINGS_DOC)
        if settings is None:
            raise ToolError("%s has no settings/%s to copy" % (label(source), operator_config.SETTINGS_DOC))
        existing = {"settings": store.documents(target_env, "settings"),
                    "lines": store.documents(target_env, "lines")}
        docs = plan(operator_config.decode_fields(settings), lines, contact)
        clashes = ["%s/%s" % (collection, doc_id) for collection, doc_id, _ in docs
                   if doc_id in existing[collection]]
        if clashes:
            raise ToolError("%s already has %s; not overwriting" % (label(wid), ", ".join(clashes)))
        for collection, doc_id, fields in docs:
            out.write("%s %s/%s %s\n" % ("write" if args.write else "would write",
                                         label(wid), "%s/%s" % (collection, doc_id),
                                         json.dumps(fields, sort_keys=True)))
        if not args.write:
            out.write("dry run: pass --write to seed %s\n" % label(wid))
            return 0
        # currentDocument.exists=false: a document created since the check above
        # fails the whole commit instead of being overwritten.
        store.commit([{"update": {"name": store.name(target_env, collection, doc_id),
                                  "fields": operator_config.encode_fields(fields)},
                       "currentDocument": {"exists": False}}
                      for collection, doc_id, fields in docs])
        out.write("seeded %s\n" % label(wid))
        return 0


def main(argv, env=None, *, opener=None, signer=None, now=None, sleep=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--workspace", required=True, help="the workspace id to seed")
    parser.add_argument("--line", action="append", default=[], metavar="ID=+NUMBER",
                        help="a WhatsApp line this workspace's bot posts from (repeatable)")
    parser.add_argument("--contact", help="the line customers are sent to (default: the first --line)")
    parser.add_argument("--settings-from", default="root",
                        help="root or workspace:<id> to copy settings/defaults from (default root)")
    parser.add_argument("--key", help="service-account key file (else GCP_SERVICE_ACCOUNT_FILE)")
    parser.add_argument("--write", action="store_true", help="write the documents")
    args = parser.parse_args(argv)
    try:
        return run(args, os.environ if env is None else env, opener=opener, signer=signer,
                   now=now, sleep=sleep, out=out)
    except ToolError as ex:
        sys.stderr.write("init-workspace: %s\n" % ex)
        return 2
    except (operator_config.ConfigUnavailable, operator_config.ConfigInvalid) as ex:
        sys.stderr.write("init-workspace: %s\n" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
