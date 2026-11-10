#!/usr/bin/env python3
"""Put an ad image in Cloudflare R2 and index it in Firestore, so swapping a
campaign's picture no longer needs a deploy.

The bot reads the index from Firestore and the bytes from R2 over plain public
HTTPS (see workspace/lib/ad_assets.py). This script is the only thing that
writes either, and it runs on the operator's machine: the R2 write credentials
never go into the container or into Railway's service variables.

Three modes, and the default writes nothing:

    upload-ad-image.py --seed-from-repo         show what the baked images imply
    upload-ad-image.py --seed-from-repo --write  upload + index all of them
    upload-ad-image.py cv ~/new-cv-ad.png --write     replace one campaign's art
    upload-ad-image.py --verify                 read Firestore back, diff the mirror

``--verify`` is the parity gate. deploy/ad-assets.json is a mirror, and a mirror
that is allowed to drift is worse than none, so this reads the index back
through the same code production uses and fails unless the two agree.

Keys are content-addressed -- ``ads/<slug>/<sha256>.<ext>`` -- so an object is
immutable, no CDN or edge cache can serve stale bytes, and a cached file's name
proves its contents. Replacing a picture writes a new object and repoints the
index; it never overwrites one.

Credentials:
  Firestore  a key with Cloud Datastore User, via --key or GCP_SERVICE_ACCOUNT_FILE.
             Use the seed key, not the production runtime key, which is read-only.
  R2         R2_ACCOUNT_ID (or --endpoint), R2_BUCKET, R2_ACCESS_KEY_ID,
             R2_SECRET_ACCESS_KEY. An S3 API token scoped to the one bucket is
             enough; it needs object write, not bucket admin.

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
import stat
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
import promo_config  # noqa: E402
import r2_sigv4  # noqa: E402

MIRROR = ROOT / "deploy" / "ad-assets.json"
MANIFEST = ROOT / "deploy" / "promo-crons.json"
CRON = ROOT / "workspace" / "cron"
RANK_STEP = operator_writer.RANK_STEP
MIRROR_VERSION = 1
MIRROR_COMMENT = (
    "Mirror of the Firestore 'assets' collection: which image each campaign "
    "rotates, its R2 key and its digest. Firestore is canonical; this file is "
    "the seed input, the record of what went live when, and what "
    "deploy/upload-ad-image.py --verify checks the store against. Generated -- "
    "edit by running that script, not by hand.")
# Magic bytes, because content_type decides how the gateway treats the file and
# a mislabelled image is refused at send time, long after the upload.
MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))


class UploadError(Exception):
    pass


# -------------------------------------------------------------------- read ----
def campaign_dirs():
    """``{slug: Path}`` for every campaign the cron manifest declares."""
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UploadError("%s is missing" % MANIFEST)
    except ValueError as ex:
        raise UploadError("%s is not valid JSON: %s" % (MANIFEST, ex))
    out = {}
    for job in manifest.get("promos") or []:
        name = job.get("name")
        slug = job.get("slug") or promo_config.promo_key(name or "")
        if name and slug:
            out[slug] = CRON / name
    if not out:
        raise UploadError("%s declares no campaigns" % MANIFEST)
    return out


def read_mirror():
    try:
        data = json.loads(MIRROR.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"campaigns": {}}
    except ValueError as ex:
        raise UploadError("%s is not valid JSON: %s" % (MIRROR, ex))
    if not isinstance(data.get("campaigns"), dict):
        raise UploadError("%s has no campaigns map" % MIRROR)
    return data


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
    collapse into a single write and lose a picture while the mirror kept both.
    Duplicate ranks and filenames are refused for the same reason the mirror test
    in CI refuses them: the rotation order and the shadow comparison both key on
    them."""
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


def declared_order(directory):
    """The image filenames the campaign's post script names, in the order it
    names them.

    Rotation is ``assets[date.today().toordinal() % len(assets)]``, so the order
    decides which picture goes out on which day. Two campaigns declare an order
    that is not alphabetical (``cv-ad.png`` before ``cv-ad-2.png``,
    ``genaie-leads.png`` before ``genaie-leads-2.png``), and seeding them sorted
    would silently shift their rotation by a day. So the order comes from the
    script, read as text rather than imported: the seed must not depend on ten
    cron modules being importable."""
    order = []
    for script in sorted(directory.glob("post*.py")):
        for name in ad_assets.SOURCE_IMAGE_RE.findall(
                script.read_text(encoding="utf-8")):
            if name not in order:
                order.append(name)
    return order


def from_repo(dirs):
    """The index the baked assets imply: every campaign's own ``assets/``,
    ranked so the rotation order matches what its post script lists today."""
    campaigns = {}
    for slug, directory in sorted(dirs.items()):
        assets = directory / ad_assets.ASSETS_DIRNAME
        if not assets.is_dir():
            continue
        present = dict((p.name, p) for p in assets.iterdir()
                       if p.is_file() and ad_assets.FILENAME_RE.match(p.name))
        order = [n for n in declared_order(directory) if n in present]
        # A file in assets/ the script never names is not in the rotation today,
        # so it must not be ranked ahead of one that is -- but it is still an
        # asset the campaign ships, so it is indexed after them.
        order += sorted(n for n in present if n not in order)
        entries, taken = [], set()
        for position, name in enumerate(order, start=1):
            doc_id = allocate_id(slug, taken)
            taken.add(doc_id)
            entry, _body = describe(slug, present[name], position * RANK_STEP,
                                    doc_id)
            entries.append(entry)
        if entries:
            validate_campaign(slug, entries)
            campaigns[slug] = entries
    return campaigns


def with_files(campaigns, slug, paths, rank=None, replace=None, disable=None,
               comment=None):
    """``campaigns`` with ``paths`` added to ``slug``, as a new campaigns map.

    Takes and returns the same shape -- the ``{slug: [entry]}`` map, not the
    whole mirror document -- so calls compose. The argument is never mutated."""
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


def bodies_for(dirs, campaigns, extra_paths):
    """``{key: body}`` for every entry whose bytes this run can actually find.

    An entry already in the mirror whose file has left the repo has nothing to
    upload -- its object is in R2 already, which ``--verify`` is there to
    confirm."""
    found = {}
    for path in extra_paths:
        body = Path(path).read_bytes()
        found[hashlib.sha256(body).hexdigest()] = body
    for slug, directory in dirs.items():
        assets = directory / ad_assets.ASSETS_DIRNAME
        if not assets.is_dir():
            continue
        for path in assets.iterdir():
            if path.is_file() and ad_assets.FILENAME_RE.match(path.name):
                body = path.read_bytes()
                found.setdefault(hashlib.sha256(body).hexdigest(), body)
    return dict((e["key"], found[e["sha256"]])
                for entries in campaigns.values() for e in entries
                if e["sha256"] in found)


# ------------------------------------------------------------------- write ----
DEFAULT_ENV_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "genaie", "r2-upload.env")


def parse_env_file(text):
    """``KEY=VALUE`` lines as a dict. Blank lines and ``#`` comments are
    ignored, as is a leading ``export``, and one layer of surrounding quotes is
    stripped -- the same shapes the entrypoint's variable sanitiser allows, so a
    value pasted from the Cloudflare dashboard behaves the same in both places.

    Not a shell: no substitution, no command execution. A file of credentials is
    the last thing that should be able to run something."""
    out = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise UploadError("line %d is not KEY=VALUE: %r" % (number, raw.strip()))
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        for quote in ('"', "'"):
            if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
                value = value[1:-1]
                break
        out[name] = value
    return out


def load_env_file(path, *, required, env=None):
    """Fill ``env`` from ``path`` without overwriting anything already set.

    Returns the path it read, or ``None``. A file holding an R2 secret that any
    other account on the machine can read is refused rather than used: the whole
    point of keeping these off the server is that exactly one place has them."""
    env = os.environ if env is None else env
    try:
        with open(path, encoding="utf-8") as handle:
            # fstat the open handle, not the path: it is the bytes just read
            # whose permissions matter, and nothing can swap the file underneath.
            mode = os.fstat(handle.fileno()).st_mode
            text = handle.read()
    except FileNotFoundError:
        if required:
            raise UploadError("no such env file: %s" % path)
        return None
    except OSError as ex:
        raise UploadError("cannot read %s: %s" % (path, ex))
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        raise UploadError("%s is readable by other accounts; run "
                          "`chmod 600 %s` and try again" % (path, path))
    try:
        values = parse_env_file(text)
    except UploadError as ex:
        raise UploadError("%s: %s" % (path, ex))
    for name, value in values.items():
        env.setdefault(name, value)
    return path


def r2_target(endpoint, bucket):
    """``<host>/<bucket>`` for the log, with the account id shortened.

    Enough for an operator to see they are pointed at the right bucket in the
    right account; not the whole account id, because this line ends up in
    terminal scrollback and pasted output."""
    host = urllib.parse.urlsplit(endpoint).netloc
    account, _, rest = host.partition(".")
    if len(account) > 8:
        host = "%s...%s.%s" % (account[:6], account[-2:], rest)
    return "%s/%s" % (host, bucket)


def r2_settings(args):
    endpoint = args.endpoint or os.environ.get("R2_ENDPOINT")
    account = os.environ.get("R2_ACCOUNT_ID")
    if not endpoint and account:
        endpoint = "https://%s.r2.cloudflarestorage.com" % account
    bucket = args.bucket or os.environ.get("R2_BUCKET")
    access = os.environ.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY")
    missing = [name for name, value in (("R2_ACCOUNT_ID or --endpoint", endpoint),
                                        ("R2_BUCKET or --bucket", bucket),
                                        ("R2_ACCESS_KEY_ID", access),
                                        ("R2_SECRET_ACCESS_KEY", secret)) if not value]
    if missing:
        raise UploadError("R2 is not configured: no %s" % ", ".join(missing))
    return endpoint, bucket, access, secret


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


def converted_rotations(campaigns, data_defined):
    """``{slug: images}`` for every data-defined campaign whose own ``images``
    list no longer matches the index being written.

    A campaign converted from this repo runs from the ``images`` on its own
    document, not from the index, so indexing a new picture has to update both
    or the swap would be uploaded, mirrored and never sent."""
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


def write_mirror(campaigns):
    payload = {"_comment": MIRROR_COMMENT, "version": MIRROR_VERSION,
               "campaigns": dict(sorted(campaigns.items()))}
    MIRROR.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")


def mirror_diff(campaigns, got):
    """Lines naming every way the store and the mirror disagree."""
    problems = []
    for slug in sorted(set(campaigns) | set(got)):
        want = dict((e["id"], fields_of(e)) for e in campaigns.get(slug) or [])
        have = dict((e["id"], fields_of(e)) for e in got.get(slug) or [])
        for doc_id in sorted(set(want) | set(have)):
            if doc_id not in have:
                problems.append("%s is in the mirror and not in Firestore" % doc_id)
            elif doc_id not in want:
                problems.append("%s is in Firestore and not in the mirror" % doc_id)
            elif want[doc_id] != have[doc_id]:
                for field in sorted(set(want[doc_id]) | set(have[doc_id])):
                    if want[doc_id].get(field) != have[doc_id].get(field):
                        problems.append("%s.%s: mirror %r, Firestore %r"
                                        % (doc_id, field, want[doc_id].get(field),
                                           have[doc_id].get(field)))
    return problems


# -------------------------------------------------------------------- main ----
def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("campaign", nargs="?", help="campaign slug, e.g. cv")
    parser.add_argument("files", nargs="*", help="image files to upload")
    parser.add_argument("--seed-from-repo", action="store_true",
                        help="index every image baked into workspace/cron/*/assets")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="apply the changes")
    mode.add_argument("--verify", action="store_true",
                      help="read Firestore back and diff the mirror")
    parser.add_argument("--prune", action="store_true",
                        help="with --write, delete index documents the mirror lacks")
    parser.add_argument("--rank", type=int, help="rotation slot for the first file")
    parser.add_argument("--replace", metavar="FILENAME",
                        help="take over this indexed image's rotation slot")
    parser.add_argument("--disable", metavar="FILENAME", action="append",
                        help="stop rotating this indexed image (repeatable)")
    parser.add_argument("--comment", help="what the new image shows")
    parser.add_argument("--key", help="service-account key file "
                                      "(else GCP_SERVICE_ACCOUNT_FILE)")
    parser.add_argument("--env-file", metavar="PATH",
                        help="credentials file (default: %s)" % DEFAULT_ENV_FILE)
    parser.add_argument("--no-env-file", action="store_true",
                        help="read credentials only from the environment")
    parser.add_argument("--endpoint", help="R2 S3 endpoint (else R2_ACCOUNT_ID)")
    parser.add_argument("--bucket", help="R2 bucket (else R2_BUCKET)")
    args = parser.parse_args(argv[1:])

    if args.prune and not args.write:
        parser.error("--prune only makes sense with --write")
    if args.seed_from_repo and (args.campaign or args.files):
        parser.error("--seed-from-repo takes the whole repo; name no campaign")
    if not args.seed_from_repo and not args.verify and not args.campaign:
        parser.error("name a campaign and its files, or pass --seed-from-repo")

    try:
        if not args.no_env_file:
            loaded = load_env_file(args.env_file or DEFAULT_ENV_FILE,
                                   required=bool(args.env_file))
            if loaded:
                print("credentials from %s" % loaded)
        dirs = campaign_dirs()
        mirror = read_mirror()
        if args.seed_from_repo:
            campaigns = from_repo(dirs)
        elif args.campaign:
            if args.campaign not in dirs:
                raise UploadError("%r is not a campaign in %s (try: %s)"
                                  % (args.campaign, MANIFEST.name,
                                     ", ".join(sorted(dirs))))
            if not args.files and not args.disable:
                raise UploadError("name at least one image file, or use --disable")
            campaigns = with_files(mirror.get("campaigns") or {}, args.campaign,
                                   args.files, rank=args.rank,
                                   replace=args.replace, disable=args.disable,
                                   comment=args.comment)
        else:
            campaigns = mirror.get("campaigns") or {}

        for slug, entries in sorted(campaigns.items()):
            validate_campaign(slug, entries)
        creds = firestore_credentials(args.key)
        with tempfile.TemporaryDirectory() as cache:
            token = token_for(creds, cache)
            rows = operator_config.list_documents(
                creds["project_id"], ad_assets.COLLECTION, token,
                opener=urllib.request.urlopen, sleep=time.sleep)
            # Console-created campaigns are canonical in Firestore, not mirrored
            # in this repo. Never flag or prune their assets as repo drift.
            campaigns_raw = operator_config.list_documents(
                creds["project_id"], "campaigns", token,
                opener=urllib.request.urlopen, sleep=time.sleep)
            data_defined = dict((slug, operator_config.decode_fields(fields))
                                for slug, fields in campaigns_raw)
            data_defined = dict((slug, doc) for slug, doc in data_defined.items()
                                if doc.get("kind") == "console")
            # A campaign converted from this repo is data-defined too, but its
            # pictures are still the mirror's business until the mirror goes.
            console_slugs = set(data_defined) - set(dirs)
            rows = [(doc_id, fields) for doc_id, fields in rows
                    if operator_config.decode_fields(fields).get("campaign") not in console_slugs]
            indexed, quarantined = ad_assets._assemble(rows)

            if args.verify:
                problems = ["%s is not a usable index document: %s" % (doc_id, why)
                            for doc_id, why in sorted(quarantined.items())]
                problems += mirror_diff(campaigns, indexed)
                for problem in problems:
                    print("DRIFT  %s" % problem)
                print("parity: %s (%d campaigns, %d images)" % (
                    "FAILED" if problems else "exact", len(indexed),
                    sum(len(v) for v in indexed.values())))
                return 1 if problems else 0

            existing = dict((e["id"], fields_of(e))
                            for entries in indexed.values() for e in entries)
            writes, strays = plan(campaigns, existing)
            bodies = bodies_for(dirs, campaigns, args.files)
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
            rotations = converted_rotations(campaigns, data_defined)
            for slug in sorted(rotations):
                print("UPDATE  campaigns/%s.images (%d picture(s))"
                      % (slug, len(rotations[slug])))
            print("%d object(s) to upload, %d document(s) to write, %d stray%s"
                  % (len(uploads), len(writes), len(strays),
                     "" if args.prune or not strays else " (kept; --prune deletes them)"))

            # Check R2 on the dry run too. Resolving it only at write time means
            # a mistyped token is first discovered with some documents already
            # written, which is the one state this is awkward to recover from.
            r2 = None
            if uploads:
                try:
                    r2 = r2_settings(args)
                except UploadError as ex:
                    if args.write:
                        raise
                    print("R2 not ready: %s" % ex)
                if r2:
                    print("R2 target: %s" % r2_target(r2[0], r2[1]))
            if not args.write:
                print("dry run: nothing uploaded or written (use --write)")
                return 0

            endpoint, bucket, access, secret = r2 if r2 else (None, None, None, None)
            for key, name, _size, content_type in uploads:
                r2_sigv4.put_object(endpoint, bucket, key, bodies[key], content_type,
                                    access_key=access, secret_key=secret)
                print("uploaded %s" % key)
            operator_writer.commit(creds["project_id"], token, writes,
                                   strays if args.prune else [])
            for slug in sorted(rotations):
                console_campaigns.set_images(creds["project_id"], token, slug,
                                             rotations[slug])
            write_mirror(campaigns)
            print("indexed in project %s; %s rewritten -- commit it"
                  % (creds["project_id"], MIRROR.relative_to(ROOT)))
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
