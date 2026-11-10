#!/usr/bin/env python3
"""Move a repository campaign's copy, schedule and pictures onto its Firestore document.

Ten campaigns still keep their captions in ``workspace/cron/<name>/post_ad.py``,
their schedule in ``deploy/promo-crons.json`` and their pictures' order in the
post script. A campaign created in the console keeps all of that on
``campaigns/<slug>`` instead and runs through ``workspace/bin/run-console-campaign``
(see ``workspace/lib/console_campaigns.py``). This script converts the first
kind into the second, one campaign at a time, so the files can be deleted.

Four modes, and the default writes nothing:

    migrate-campaigns-to-firestore.py --only cv            what --write would save
    migrate-campaigns-to-firestore.py --only cv --write    convert it
    migrate-campaigns-to-firestore.py --all --verify       0 only if each is converted,
                                                           still matches this repo and
                                                           has a verified schedule
    migrate-campaigns-to-firestore.py --only cv --revert   hand it back to its script

What a conversion writes onto the existing document: ``kind: "console"``, a
display ``name``, ``captions`` (an override already saved from the console
wins), ``tracking``, ``schedule``, ``timeoutSeconds``, ``failureAlertTo``,
``images`` (the enabled entries of the ``assets`` index, in rotation order),
``jobName`` and ``scheduleStatus: "pending"``. ``enabled`` becomes the store's
switch AND the manifest's, because a data-defined campaign has only the one.
``comment``, ``weekly_comment``, ``dir`` and ``rank`` are left alone.

``jobName`` is the campaign's existing cron job name. The console API's
reconciler finds that job by name within five minutes and edits it in place to
run ``run-console-campaign <slug>``; the run then uses the same
``runtime/cron-state/<jobName>/state.json``, so sent-today state and the
caption-rotation counters carry straight over. Nothing moves on the volume.

The conversion changes where a run reads its inputs, not the inputs: the same
engine is handed the same captions, tracking, picture order and timeout. So the
dry run refuses anything that would make them differ -- a picture the script
names that the index does not, a different order, an on-the-hour schedule, more
groups than the timeout can reach at full spacing.

Convert in a quiet window, at least ten minutes before the campaign's slot, and
check ``--verify`` before it. Do not re-run a converted campaign's
``deploy/install-*-promo.sh``: that points the job back at the script, and the
reconciler will point it forward again. ``--revert`` followed by the installer
is the way back while the script still exists.

Credentials: a service-account key with Cloud Datastore User, given by --key or
GCP_SERVICE_ACCOUNT_FILE. Use the seed key on your own machine, never the
runtime key, which is read-only.

Exit codes: 0 done / verified, 1 not converted or a write failed, 2 bad input.
"""

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "workspace" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import ad_assets  # noqa: E402
import console_campaigns  # noqa: E402
import operator_config  # noqa: E402
import operator_writer  # noqa: E402

MANIFEST = ROOT / "deploy" / "promo-crons.json"
WORKSPACE = ROOT / "workspace"
KIND = "console"
# What ``--revert`` leaves behind. Anything other than "console" reads as a
# script campaign everywhere; a word says why the data-defined fields are there.
REVERTED_KIND = "manifest"
# The fields a conversion owns. Everything else on the document is the
# operator's and is carried through untouched.
OWNED = ("kind", "name", "enabled", "captions", "tracking", "schedule",
         "timeoutSeconds", "failureAlertTo", "images", "jobName")
ACRONYMS = {"cv": "CV"}


class MigrationError(Exception):
    pass


# ------------------------------------------------------------------ repo ----
def load_manifest(path=MANIFEST):
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MigrationError("%s is gone -- there is nothing left to convert" % path)
    except ValueError as ex:
        raise MigrationError("%s is not valid JSON: %s" % (path, ex))
    if not isinstance(manifest.get("promos"), list) or not manifest["promos"]:
        raise MigrationError("%s declares no campaigns" % path)
    return manifest


def script_path(script, workspace=WORKSPACE):
    """Manifest scripts are absolute container paths; re-root them on this
    checkout, as ``workspace/bin/console-read`` does."""
    marker = "/workspace/"
    index = script.find(marker)
    return Path(script) if index == -1 else Path(workspace) / script[index + len(marker):]


def import_campaign(path):
    """A campaign's post script, for its copy and tracking. Every one guards its
    entry point with ``if __name__ == "__main__"``, so importing sends nothing."""
    spec = importlib.util.spec_from_file_location("_campaign_%s" % path.parent.name, path)
    if spec is None or spec.loader is None:
        raise MigrationError("cannot import campaign module %s" % path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except FileNotFoundError:
        raise MigrationError("%s is gone -- this campaign has no script to convert" % path)
    return module


def _as_list(value):
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def declared(entry, workspace=WORKSPACE):
    """What the repository says a campaign sends: ``(captions, tracking,
    image filenames in rotation order)``."""
    module = import_campaign(script_path(entry["script"], workspace))
    captions = [str(text) for text in _as_list(getattr(module, "POST_TEXTS", None))]
    tracking = dict(getattr(module, "TRACKING", {}) or {})
    images = getattr(module, "IMAGE", None)
    if images is None:
        images = getattr(module, "IMAGE_PATH", None)
    return captions, tracking, [os.path.basename(str(image)) for image in _as_list(images)]


def schedule_of(entry):
    """``{"time", "timezone", "weekdays"}`` from a daily ``M H * * *`` cron."""
    fields = str(entry.get("cron", "")).split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        raise MigrationError("%s: only daily 'M H * * *' schedules can be converted, got %r"
                             % (entry.get("name"), entry.get("cron")))
    try:
        minute, hour = int(fields[0]), int(fields[1])
    except ValueError:
        raise MigrationError("%s: bad cron time %r" % (entry.get("name"), entry.get("cron")))
    if not (0 <= minute < 60 and 0 <= hour < 24):
        raise MigrationError("%s: bad cron time %r" % (entry.get("name"), entry.get("cron")))
    return {"time": "%02d:%02d" % (hour, minute), "timezone": entry.get("tz") or "UTC",
            "weekdays": list(range(7))}


def display_name(slug):
    return " ".join(ACRONYMS.get(word, word.capitalize()) for word in slug.split("-"))


def manifest_enabled(entry):
    value = entry.get("enabled", True)
    if type(value) is not bool:
        raise MigrationError("%s has non-boolean enabled=%r" % (entry.get("slug"), value))
    return value


# ------------------------------------------------------------------ plan ----
def desired(entry, manifest, before, assets, workspace=WORKSPACE):
    """The campaign document a conversion would save, from the one that is there
    (``before``) and this campaign's enabled index entries in rotation order
    (``assets``). Raises :class:`MigrationError` for anything that would make
    the converted campaign send something other than what it sends today."""
    slug = entry["slug"]
    captions, tracking, filenames = declared(entry, workspace)
    indexed = [asset["filename"] for asset in assets]
    if filenames != indexed:
        raise MigrationError(
            "%s: the script rotates %s but the asset index has %s; fix the index "
            "(deploy/upload-ad-image.py) before converting"
            % (slug, ", ".join(filenames) or "nothing", ", ".join(indexed) or "nothing"))
    store_switch = before.get("enabled", True)
    if type(store_switch) is not bool:
        raise MigrationError("%s: the store's enabled is %r, not a boolean" % (slug, store_switch))
    after = dict(before)
    after.update({
        "kind": KIND,
        "name": before.get("name") if before.get("kind") == KIND else display_name(slug),
        "enabled": store_switch and manifest_enabled(entry),
        # An override saved from the console is already what a run sends.
        "captions": before["captions"] if "captions" in before else captions,
        "tracking": tracking,
        "schedule": schedule_of(entry),
        "timeoutSeconds": int(entry.get("timeoutSeconds") or manifest.get("timeoutSeconds") or 7200),
        "failureAlertTo": entry.get("failureAlertTo") or "",
        "images": console_campaigns.images_from_assets(assets),
        "jobName": entry["name"],
    })
    if not console_campaigns.JOB_NAME.fullmatch(str(entry.get("name") or "")):
        raise MigrationError("%s: %r is not a usable job name" % (slug, entry.get("name")))
    try:
        console_campaigns.validate_definition(slug, after)
    except operator_writer.WriteRefused as ex:
        raise MigrationError("%s: %s" % (slug, ex))
    return after


def differences(before, after):
    """The owned fields a conversion would change, as ``[(name, old, new)]``."""
    return [(name, before.get(name), after.get(name)) for name in OWNED
            if before.get(name) != after.get(name)]


def _short(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 100 else text[:97] + "..."


def enabled_assets(store, slug):
    """This campaign's enabled index entries, in the order a run rotates them."""
    rows = store.list(ad_assets.COLLECTION, where={"fieldFilter": {
        "field": {"fieldPath": "campaign"}, "op": "EQUAL", "value": {"stringValue": slug}}})
    campaigns, quarantined = ad_assets._assemble(rows)
    if quarantined:
        raise MigrationError("%s: unusable asset index document(s): %s" % (
            slug, "; ".join("%s (%s)" % item for item in sorted(quarantined.items()))))
    return [entry for entry in campaigns.get(slug, []) if entry.get("enabled")]


def group_count(store, slug):
    return len(store.list("groups", where=operator_config.campaign_groups_filter(slug)))


# ----------------------------------------------------------------- modes ----
def convert(entry, manifest, store, *, write, actor, workspace=WORKSPACE, out=sys.stdout):
    """Plan one conversion and, with ``write``, commit it. Returns whether the
    campaign is (now) converted."""
    slug = entry["slug"]
    # A dry run reads outside a transaction, so a read-only key can preview.
    if write:
        store.begin()
    try:
        before = store.get("campaigns", slug)
        if before is None:
            raise MigrationError("campaigns/%s does not exist; seed the store first" % slug)
        if before.get("deleted"):
            raise MigrationError("campaign was deleted; conversion is refused")
        if before.get("kind") == KIND:
            out.write("%s: already converted; nothing to write\n" % slug)
            return True
        after = desired(entry, manifest, before, enabled_assets(store, slug), workspace)
        count = group_count(store, slug)
        problem = console_campaigns.spacing_problem(count, after["timeoutSeconds"])
        if problem:
            raise MigrationError("%s: %s" % (slug, problem))
        after["scheduleStatus"] = "pending"
        out.write("%s: %d group(s), %s %s, %d caption(s), %d picture(s), job %s\n" % (
            slug, count, after["schedule"]["time"], after["schedule"]["timezone"],
            len(after["captions"]), len(after["images"]), after["jobName"]))
        for name, old, new in differences(before, after):
            out.write("  %-15s %s -> %s\n" % (name, _short(old), _short(new)))
        if not write:
            return False
        audit_id, audit = operator_writer.audit_document(actor, "campaigns", slug, before, after)
        store.commit([("campaigns", slug, after, True),
                      (operator_writer.AUDIT_COLLECTION, audit_id, audit, False)])
        out.write("%s: converted; the reconciler repoints job %s within five minutes\n"
                  % (slug, after["jobName"]))
        return True
    finally:
        store.rollback()


def verify(entry, manifest, store, workspace=WORKSPACE, out=sys.stdout):
    """Whether one campaign is converted, still says what this repository says,
    and has had its gateway schedule verified by the reconciler."""
    slug = entry["slug"]
    doc = store.get("campaigns", slug)
    if doc is None or doc.get("kind") != KIND:
        out.write("DRIFT  %s is not converted\n" % slug)
        return False
    problems = ["%s.%s: repository %s, Firestore %s" % (slug, name, _short(new), _short(old))
                for name, old, new in differences(
                    doc, desired(entry, manifest, doc, enabled_assets(store, slug), workspace))
                # Both are the operator's to change after a conversion.
                if name not in ("enabled", "name")]
    if doc.get("scheduleStatus") != "ready":
        problems.append("%s: gateway schedule is %r, not verified yet"
                        % (slug, doc.get("scheduleStatus")))
    for problem in problems:
        out.write("DRIFT  %s\n" % problem)
    return not problems


def revert(entry, store, *, write, actor, out=sys.stdout):
    slug = entry["slug"]
    if write:
        store.begin()
    try:
        before = store.get("campaigns", slug)
        if before is None or before.get("kind") != KIND:
            out.write("%s: not converted; nothing to revert\n" % slug)
            return True
        after = dict(before, kind=REVERTED_KIND)
        out.write("%s: kind console -> %s; then run %s at once -- until it has, job %s "
                  "runs a campaign that no longer exists and its failure alert fires\n"
                  % (slug, REVERTED_KIND, entry.get("installer"), entry["name"]))
        if not write:
            return False
        audit_id, audit = operator_writer.audit_document(actor, "campaigns", slug, before, after)
        store.commit([("campaigns", slug, after, True),
                      (operator_writer.AUDIT_COLLECTION, audit_id, audit, False)])
        return True
    finally:
        store.rollback()


def open_store(key, cache_dir):
    env = dict(os.environ)
    if key:
        env["GCP_SERVICE_ACCOUNT_FILE"] = key
    # Never pick up a runtime key by accident: this script names its key, and
    # its token goes in a throwaway cache, never the runtime one.
    env.pop("GCP_SERVICE_ACCOUNT_B64", None)
    env["OPENCLAW_STATE_DIR"] = cache_dir
    if operator_config.credentials(env) is None:
        raise MigrationError("no key: pass --key or set GCP_SERVICE_ACCOUNT_FILE")
    return console_campaigns.Store(env)


def selected(manifest, only, everything):
    promos = manifest["promos"]
    if everything:
        return promos
    known = dict((promo["slug"], promo) for promo in promos)
    unknown = [slug for slug in only if slug not in known]
    if unknown:
        raise MigrationError("%s: not in %s (try: %s)" % (
            ", ".join(unknown), MANIFEST.name, ", ".join(sorted(known))))
    return [known[slug] for slug in only]


def main(argv, store=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--key", help="service-account key file (else GCP_SERVICE_ACCOUNT_FILE)")
    parser.add_argument("--only", action="append", default=[], metavar="SLUG",
                        help="campaign to act on (repeatable)")
    parser.add_argument("--all", action="store_true", help="every declared campaign")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true",
                      help="exit 0 only if each campaign is converted, matches this "
                           "repository and has a verified schedule")
    mode.add_argument("--revert", action="store_true",
                      help="hand a converted campaign back to its script")
    parser.add_argument("--write", action="store_true", help="apply the changes")
    parser.add_argument("--actor", default=os.environ.get("USER") or "migration",
                        help="name recorded in the audit trail")
    args = parser.parse_args(argv[1:])
    if bool(args.only) == bool(args.all):
        parser.error("name campaigns with --only, or pass --all")
    if args.verify and args.write:
        parser.error("--verify writes nothing; drop --write")
    if args.all and args.write:
        parser.error("convert one campaign at a time: name it with --only")

    try:
        manifest = load_manifest()
        entries = selected(manifest, args.only, args.all)
        with tempfile.TemporaryDirectory() as cache:
            store = store or open_store(args.key, cache)
            if args.verify:
                results = [verify(entry, manifest, store) for entry in entries]
                print("converted: %d of %d" % (sum(results), len(results)))
                return 0 if all(results) else 1
            for entry in entries:
                if args.revert:
                    revert(entry, store, write=args.write, actor=args.actor)
                else:
                    convert(entry, manifest, store, write=args.write, actor=args.actor)
            if not args.write:
                print("dry run: nothing written (use --write)")
            return 0
    except MigrationError as ex:
        sys.stderr.write("migrate-campaigns: %s\n" % ex)
        return 2
    except (operator_config.ConfigUnavailable, operator_config.ConfigInvalid,
            operator_writer.WriteRefused) as ex:
        sys.stderr.write("migrate-campaigns: %s\n" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
