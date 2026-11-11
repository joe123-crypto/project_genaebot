#!/usr/bin/env python3
"""Where a campaign's ad images come from: Cloudflare R2, through a verified cache.

The images used to be the one thing in ``workspace/`` that only a deploy could
change. They shipped inside the container and were rsynced onto the volume on
every boot, so swapping a picture cost a commit, a PR, CI, a Docker build and a
gateway restart. The bytes now live in Cloudflare R2 and the index of them in
Firestore, so a swap is an upload plus a console edit.

There used to be a choice here, behind ``GENAIE_AD_ASSETS_SOURCE``: the baked
assets, the baked assets plus a background comparison, or R2. The baked assets
are gone -- they were operator data in a code repository -- so the variable
selects nothing and is no longer read, and with them went the committed mirror
of the index, the shadow comparison, and every path that answered a failed read
by sending the picture inside the container.

What is left is one fallback and it is a real one: a failed live read serves the
last-known-good snapshot from ``runtime/firestore/``, bounded by
:data:`INDEX_MAX_AGE_SECONDS`. Past that there is nothing to send, and the
campaign stands down loudly rather than posting an advert nobody chose.

:func:`ensure` is the whole surface a run touches: it is handed one index entry
and hands back a verified local path. ``workspace/lib/console_campaigns.py``
calls it once per picture, all of them or none, before a single group is posted
to.

**The cache is the point, not an optimization.** ``openclaw message send``
accepts an ``https://`` URL for ``--media`` and would fetch it itself, and that
is deliberately not what happens here, for four reasons:

* ``promo_broadcast.send_ad`` is called once per group, so a URL would refetch
  the same two megabytes for every group in the run -- and give every group its
  own chance to fail.
* Failing mid-run is the expensive kind. The engine deliberately marks a group
  sent rather than retry an uncertain send, because re-sending double-posts.
* ``os.path.exists`` in ``promo_broadcast.broadcast`` is the engine's only
  pre-flight guard. A URL fails it, so the guard would have to be special-cased
  away -- losing the one thing that makes this change safe.
* One download plus one digest check proves the content *before the first send*.
  A URL handoff can serve a truncated body to the seventeenth group.

So: download once, verify the digest, hand over a local path. Please do not
"simplify" this into passing the URL through.

The cache lives under the state volume's ``media/`` tree, NOT under ``runtime/``,
because openclaw only reads local media from an allowlist of roots.
``buildMediaLocalRoots`` in openclaw 2026.9.9, the version the image pins, returns
exactly the temp dir, ``<config>/media``, ``<state>/media``, the delivery queue's
media dir, ``<state>/canvas``, ``<state>/workspace`` and ``<state>/sandboxes`` --
and the outbound send path uses the variant that does not expand that set for the
path it is given. ``runtime/`` is not on the list, and ``workspace/`` is wiped on
every boot, so ``media/`` is the only tier that is both durable and readable.

Std-lib only, so it stays importable from the OpenClaw ``--command`` cron
scripts, which are std-lib only by contract.

Run by hand for a one-off check of every campaign (exits 1 on drift)::

    python3 workspace/lib/ad_assets.py
"""

import datetime
import functools
import hashlib
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import operator_config  # noqa: E402
import promo_config  # noqa: E402

BASE_VAR = "GENAIE_AD_ASSETS_BASE_URL"
DIR_VAR = "GENAIE_AD_ASSETS_DIR"

COLLECTION = "assets"
PREFIX = "[ad-assets]"
# The index snapshot is small, must outlive a media sweep, and belongs with the
# other Firestore state -- so it goes in the runtime tier beside
# operator-config.json, not in the byte cache under media/.
INDEX_FILE = "ad-assets-index.json"
CACHE_SUBDIR = os.path.join("media", "ad-assets")

# openclaw's MAX_IMAGE_BYTES. An image over this is refused rather than sent and
# silently re-encoded, because the ad's legibility is the ad.
MAX_BYTES = 6 * 1024 * 1024
CONTENT_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}

# ``key`` arrives from Firestore and goes into a URL, so it is pinned rather
# than trusted: without this a row reading "//elsewhere.example/x.jpg" would
# turn the index into a request-forgery primitive. Every key this repo writes is
# content-addressed, so the pattern can be exact.
KEY_RE = re.compile(r"^ads/[a-z0-9][a-z0-9-]*/([0-9a-f]{64})\.(jpg|png)$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(jpg|jpeg|png)$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# How stale the index snapshot may be before it stops counting as an index. The
# same bound operator_config puts on its own snapshot, for the same reason: a
# day's outage is survivable, a week's means acting on an index someone has
# since changed.
INDEX_MAX_AGE_SECONDS = operator_config.SNAPSHOT_MAX_AGE_SECONDS

# How long :func:`catalogue` will describe the index from the snapshot before
# reading Firestore again. It exists for the operator console, which asks on
# every page load and again every 60 seconds per open tab; without a window
# every one of those would be a token fetch and a ``list_documents``, against a
# project already answering 429 on the operator-config path. A picture that
# changed five minutes ago showing five minutes late costs nothing; a console
# that rate-limits the thing it is monitoring costs the monitoring.
CATALOGUE_MAX_AGE_SECONDS = 300

# How long a failed live read suppresses the next one. Without it every console
# refresh retries a Firestore that has already said no -- and the failure this
# exists for is a project whose daily quota is exhausted, where retrying on a
# loop is both useless and part of the problem. One attempt per window is
# enough to notice the quota coming back.
RETRY_AFTER_SECONDS = CATALOGUE_MAX_AGE_SECONDS
FAILURE_FILE = "ad-assets-unreadable.json"

# Cloudflare's r2.dev public endpoint answers 403 to a request whose User-Agent
# is urllib's default "Python-urllib/3.x" -- and 200 to any other value, an empty
# one included. Measured against the live bucket on 2026-11-08: curl, a named
# agent and no agent at all all succeed, "Python-urllib/3.13" does not. Without
# this header every fetch fails, which with nothing baked to fall back to means
# the campaign stands down. Name the bot rather than impersonate a browser.
USER_AGENT = "genaebot-ad-assets/1"
HTTP_TIMEOUT_SECONDS = 20
RETRY_BACKOFF_SECONDS = 1.0
RETRYABLE_STATUS = operator_config.RETRYABLE_STATUS
class AssetsUnavailable(Exception):
    """No live index and no usable snapshot: nothing says what to send."""


class AssetUnusable(Exception):
    """One asset cannot be served -- missing, oversized, or the wrong bytes."""


# ------------------------------------------------------------------- setup ----
def cache_dir(env=None):
    """The verified byte cache. Durable across deploys, and inside the set of
    roots openclaw will read local media from."""
    env = os.environ if env is None else env
    override = env.get(DIR_VAR)
    if override:
        return override
    root = env.get("OPENCLAW_STATE_DIR") or operator_config.DEFAULT_STATE_DIR
    return os.path.join(root, CACHE_SUBDIR)


def base_url(env=None):
    """Where the bytes are served from, or ``None`` when it is not configured.

    This stays in the environment deliberately. Were it a field in the index, a
    single Firestore write could point every image fetch at another host; as an
    env var, redirecting the fetches needs access to the Railway service."""
    env = os.environ if env is None else env
    raw = (env.get(BASE_VAR) or "").strip().rstrip("/")
    if not raw:
        return None
    if not raw.startswith("https://"):
        raise AssetsUnavailable("%s must start with https:// (got %r)"
                                % (BASE_VAR, env.get(BASE_VAR)))
    return raw


def public_url(entry, env=None):
    """Where ``entry``'s bytes are served from, or ``None`` when :data:`BASE_VAR`
    is not set.

    One place builds this string. :func:`ensure` fetches it and the operator
    console displays it, and the two must not be able to disagree about where a
    picture lives -- a console showing one object while a run sends another is
    worse than a console showing nothing."""
    root = base_url(env)
    return None if root is None else "%s/%s" % (root, entry["key"])


def index_path(env=None):
    env = os.environ if env is None else env
    slug = env.get("GENAIE_AD_ASSETS_SCOPE")
    if slug:
        import hashlib
        return os.path.join(operator_config.state_dir(env), "asset-scopes",
                            hashlib.sha256(slug.encode()).hexdigest(), INDEX_FILE)
    return os.path.join(operator_config.state_dir(env), INDEX_FILE)


def _log(message, err=None):
    (err or sys.stderr).write("%s %s\n" % (PREFIX, message))


def _stamp(now):
    at = datetime.datetime.fromtimestamp(now(), datetime.timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------- index ----
def _entry(doc_id, fields):
    """One validated index entry, or :class:`ValueError` saying what is wrong.

    Every field is checked rather than trusted. The index is editable from the
    Firebase Console, so a typo there must cost one asset, never a campaign."""
    def need(name, kind):
        value = fields.get(name)
        if not isinstance(value, kind) or isinstance(value, bool) != (kind is bool):
            raise ValueError("%s: %s is %r" % (doc_id, name, value))
        return value

    campaign = need("campaign", str)
    if not SLUG_RE.match(campaign):
        raise ValueError("%s: campaign %r is not a slug" % (doc_id, campaign))
    filename = os.path.basename(need("filename", str))
    if not FILENAME_RE.match(filename):
        raise ValueError("%s: filename %r is not a plain image name"
                         % (doc_id, fields.get("filename")))
    key = need("key", str)
    matched = KEY_RE.match(key)
    if not matched:
        raise ValueError("%s: key %r is not ads/<slug>/<sha256>.<ext>" % (doc_id, key))
    sha256 = need("sha256", str)
    if not SHA_RE.match(sha256):
        raise ValueError("%s: sha256 %r is not 64 hex characters" % (doc_id, sha256))
    # The key embeds the digest, so the two cannot disagree without one of them
    # being wrong -- and which one is anybody's guess, so refuse the row.
    if matched.group(1) != sha256:
        raise ValueError("%s: key names digest %s but sha256 is %s"
                         % (doc_id, matched.group(1), sha256))
    size = need("bytes", int)
    if not 0 < size <= MAX_BYTES:
        raise ValueError("%s: bytes is %d, outside 1..%d" % (doc_id, size, MAX_BYTES))
    content_type = need("content_type", str)
    if content_type not in CONTENT_TYPES:
        raise ValueError("%s: content_type %r is not one of %s"
                         % (doc_id, content_type, ", ".join(sorted(CONTENT_TYPES))))
    if ".%s" % matched.group(2) != CONTENT_TYPES[content_type]:
        raise ValueError("%s: key ends .%s but content_type is %s"
                         % (doc_id, matched.group(2), content_type))
    rank = fields.get(operator_config.RANK)
    if rank is not None and (not isinstance(rank, int) or isinstance(rank, bool)):
        raise ValueError("%s: rank is %r" % (doc_id, rank))
    enabled = fields.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("%s: enabled is %r" % (doc_id, enabled))
    return {"id": doc_id, "campaign": campaign, "filename": filename, "key": key,
            "sha256": sha256, "bytes": size, "content_type": content_type,
            "rank": rank, "enabled": enabled,
            "comment": fields.get("comment") if isinstance(fields.get("comment"), str)
            else None}


def _assemble(rows):
    """``({slug: [entry]}, {doc_id: problem})`` from raw Firestore documents.

    Decoding is per document on purpose. ``operator_config.assemble`` raises for
    an undecodable document because a bad group would otherwise be advertised
    to; here a bad row is one picture, and the other nine campaigns have no
    business failing over it."""
    campaigns, quarantined = {}, {}
    decoded = []
    for doc_id, fields in rows:
        try:
            decoded.append((doc_id, operator_config.decode_fields(fields)))
        except ValueError as ex:
            quarantined[doc_id] = str(ex)
    # Decode BEFORE ranking, the order operator_config.assemble uses. ``rows``
    # carry Firestore's typed values, so a rank arrives as
    # ``{"integerValue": "20"}``; ``_ranked`` only recognizes a decoded int and
    # would quietly fall back to sorting every document by its id. That reads
    # correctly for ids cv-1..cv-3 and lies as soon as an operator re-ranks, or
    # a campaign reaches ten images and cv-10 sorts before cv-2.
    for doc_id, fields in operator_config._ranked(decoded):
        try:
            entry = _entry(doc_id, fields)
        except ValueError as ex:
            quarantined[doc_id] = str(ex)
            continue
        campaigns.setdefault(entry["campaign"], []).append(entry)
    return campaigns, quarantined


def index(env=None, *, opener=None, signer=None, now=None, sleep=None, log=None):
    """The index to act on::

        {"source": "firestore" | "snapshot",
         "fetchedAt": ISO-8601 UTC,
         "degraded": None | why Firestore itself was not read,
         "campaigns": {slug: [entry, ...]},   # enabled only, in rotation order
         "quarantined": {doc_id: problem}}

    A live read refreshes the snapshot; a failed one falls back to it, bounded by
    :data:`INDEX_MAX_AGE_SECONDS`. Raises :class:`AssetsUnavailable` when neither
    is usable -- and the caller then has nothing to send at all, which is why
    a run treats it as a failure rather than posting something unchosen."""
    env = os.environ if env is None else env
    now = now or time.time
    sleep = sleep or time.sleep
    log = log or (lambda message: _log(message))
    try:
        creds = operator_config.credentials(env)
        if creds is None:
            raise AssetsUnavailable("no service account configured "
                                    "(GCP_SERVICE_ACCOUNT_B64 or GCP_SERVICE_ACCOUNT_FILE)")
        token = operator_config.access_token(
            creds, operator_config.token_cache_path(operator_config.state_dir(env)),
            opener=opener or urllib.request.urlopen,
            signer=signer or operator_config.node_signer, now=now, sleep=sleep)
        slug = env.get("GENAIE_AD_ASSETS_SCOPE")
        if slug:
            rows = operator_config.query_documents(
                creds["project_id"], COLLECTION,
                {"fieldFilter": {"field": {"fieldPath": "campaign"}, "op": "EQUAL",
                                 "value": {"stringValue": slug}}}, token,
                opener=opener or urllib.request.urlopen, sleep=sleep)
        else:
            rows = operator_config.list_documents(
                creds["project_id"], COLLECTION, token,
                opener=opener or urllib.request.urlopen, sleep=sleep)
    except (operator_config.ConfigUnavailable, AssetsUnavailable) as ex:
        log("index unreadable (%s); using the last-known-good snapshot" % ex)
        return _from_snapshot(env, str(ex), now=now)

    campaigns, quarantined = _assemble(rows)
    for doc_id, problem in sorted(quarantined.items()):
        log("ignoring asset %s: %s" % (doc_id, problem))
    current = {"fetchedAt": _stamp(now), "campaigns": campaigns,
               "quarantined": quarantined}
    try:
        operator_config._write_private_json(index_path(env), current)
    except OSError as ex:
        log("could not write %s: %s" % (index_path(env), ex))
    return dict(current, source="firestore", degraded=None,
                campaigns=_enabled_only(campaigns))


def _enabled_only(campaigns):
    return dict((slug, [e for e in entries if e.get("enabled")])
                for slug, entries in campaigns.items())


def _from_snapshot(env, why, now=None):
    now = now or time.time
    path = index_path(env)
    snapshot = operator_config._read_json(path)
    if not isinstance(snapshot, dict) and (env or {}).get("GENAIE_AD_ASSETS_SCOPE"):
        original = dict(env)
        original.pop("GENAIE_AD_ASSETS_SCOPE", None)
        snapshot = operator_config._read_json(index_path(original))
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("campaigns"), dict):
        raise AssetsUnavailable("the index is unreadable (%s) and there is no snapshot "
                                "at %s" % (why, path))
    age = operator_config._snapshot_age_seconds(snapshot, now)
    if age is None:
        raise AssetsUnavailable("the index is unreadable (%s) and the snapshot at %s "
                                "does not say when it was taken" % (why, path))
    if age > INDEX_MAX_AGE_SECONDS:
        raise AssetsUnavailable(
            "the index is unreadable (%s) and the snapshot is %.0f h old, past the "
            "%.0f h limit" % (why, age / 3600.0, INDEX_MAX_AGE_SECONDS / 3600.0))
    campaigns, quarantined = _from_json(snapshot["campaigns"],
                                        snapshot.get("quarantined"))
    if (env or {}).get("GENAIE_AD_ASSETS_SCOPE"):
        slug = env["GENAIE_AD_ASSETS_SCOPE"]
        campaigns = {slug: campaigns[slug]} if slug in campaigns else {}
    return {"source": "snapshot", "fetchedAt": snapshot.get("fetchedAt"),
            "degraded": why, "campaigns": _enabled_only(campaigns),
            "quarantined": quarantined}


def _from_json(campaigns_json, quarantined=None):
    """``({slug: [entry]}, {id: problem})`` out of plain JSON.

    The snapshot is a file that something other than this process last wrote,
    so it is not trusted: every row goes back through :func:`_entry`."""
    campaigns, quarantined = {}, dict(quarantined or {})
    for slug, entries in (campaigns_json or {}).items():
        kept = []
        for entry in entries if isinstance(entries, list) else []:
            try:
                kept.append(_entry(entry.get("id", "?"), entry))
            except (ValueError, AttributeError) as ex:
                quarantined[str(entry)[:80]] = str(ex)
        if kept:
            campaigns[slug] = kept
    return campaigns, quarantined


def failure_path(env=None):
    return os.path.join(operator_config.state_dir(env), FAILURE_FILE)


def _recently_failed(env, now):
    """Whether a live read failed inside the retry window, and why.

    Kept on disk rather than in memory because every caller that matters is a
    fresh process: an in-memory flag would stop nothing."""
    stamp = operator_config._read_json(failure_path(env))
    if not isinstance(stamp, dict):
        return None
    at = stamp.get("at")
    if not isinstance(at, (int, float)) or now() - at > RETRY_AFTER_SECONDS:
        return None
    return str(stamp.get("why") or "a recent read failed")


def _note_failure(env, why, now, log):
    try:
        operator_config._write_private_json(failure_path(env),
                                            {"at": now(), "why": why})
    except OSError as ex:
        log("could not write %s: %s" % (failure_path(env), ex))


def _clear_failure(env):
    try:
        os.remove(failure_path(env))
    except OSError:
        pass


def _recent_snapshot(env, max_age, now):
    """The last-known-good index when it is younger than ``max_age``, else
    ``None``.

    Unlike :func:`_from_snapshot` this is not a fallback for a read that failed
    -- it is what stops the read happening at all -- so an absent, unreadable or
    stale snapshot is simply "no", never an error, and the result is not marked
    degraded: reusing a fresh snapshot is the normal path, not a lesser one."""
    if not max_age:
        return None
    try:
        result = _from_snapshot(env, "not read: the snapshot is still fresh", now=now)
    except AssetsUnavailable:
        return None
    age = operator_config._snapshot_age_seconds(
        {"fetchedAt": result.get("fetchedAt")}, now)
    if age is None or age > max_age:
        return None
    return dict(result, degraded=None)


def _stale_snapshot(env, why, now):
    """The last-known-good index with no age limit but
    :data:`INDEX_MAX_AGE_SECONDS`, for a window in which no live read is
    attempted. ``None`` when no snapshot is usable, which is the one case with
    genuinely nothing to show.

    This is the other half of :data:`RETRY_AFTER_SECONDS`: suppressing the read
    must not also throw away the snapshot that read would have fallen back to,
    or the backoff would cost the fresher answer it is trying to protect."""
    try:
        return _from_snapshot(env, why, now=now)
    except AssetsUnavailable:
        return None


def catalogue(env=None, *, max_age=CATALOGUE_MAX_AGE_SECONDS, now=None, log=None,
              **inject):
    """What the index says, for a caller that wants to DESCRIBE a campaign's
    pictures rather than send them::

        {"campaigns": {slug: [entry + {"url": ...}]},  # enabled, rotation order
         "source": "firestore" | "snapshot" | None,
         "fetchedAt": ISO-8601 UTC | None,
         "note": None | why there is nothing to show}

    Two things separate it from :func:`index`:

    * **It never raises.** Its caller is the console read bridge, where a
      campaign's pictures are one card on one page. An unreadable index must
      cost the pictures and nothing else, so every failure arrives as ``note``
      beside an empty ``campaigns`` -- never as a campaigns screen that will not
      load.
    * **It prefers a recent snapshot to a live read**, see
      :data:`CATALOGUE_MAX_AGE_SECONDS`. :func:`index` writes that snapshot as a
      side effect, so the window keeps refilling itself.

    ``url`` is ``None`` on every entry when :data:`BASE_VAR` is unset, which is
    a different thing from having no pictures -- the index still names them --
    and ``note`` says so."""
    env = os.environ if env is None else env
    now = now or time.time
    log = log or (lambda message: _log(message))

    current = _recent_snapshot(env, max_age, now)
    if current is None:
        # A read that failed moments ago is not worth repeating on every
        # refresh, and the case this guards -- an exhausted Firestore quota --
        # is one where retrying in a loop is part of what keeps it exhausted.
        why = _recently_failed(env, now)
        if why is None:
            try:
                current = index(env, now=now, log=log, **inject)
            except AssetsUnavailable as ex:
                why = str(ex)
            else:
                # A live read that failed over a snapshot still inside the 48 h
                # limit comes back as an answer rather than an exception, with
                # ``degraded`` naming the failure. It is still a failed read, so
                # the window has to start here too -- otherwise the one case
                # that always has a snapshot, every deployed container, never
                # backs off at all.
                why = current.get("degraded")
            if why is None:
                _clear_failure(env)
            else:
                _note_failure(env, why, now, log)
        else:
            current = _stale_snapshot(env, why, now)
        if current is None:
            return {"campaigns": {}, "source": None, "fetchedAt": None, "note": why}

    # Resolved once, outside the loop: a bad setting is one answer about the
    # whole index, not the same complaint repeated per picture.
    try:
        root = base_url(env)
    except AssetsUnavailable as ex:
        root, setting = None, str(ex)
    else:
        setting = None if root is not None else (
            "%s is not set, so the index names each campaign's pictures but no "
            "address to fetch them from" % BASE_VAR)

    campaigns = dict(
        (slug, [dict(entry, url=None if root is None else public_url(entry, env))
                for entry in entries])
        for slug, entries in current["campaigns"].items())
    reasons = [reason for reason in (current.get("degraded"), setting) if reason]
    return {"campaigns": campaigns, "source": current.get("source"),
            "fetchedAt": current.get("fetchedAt"),
            "note": "; ".join(reasons) or None}


# ------------------------------------------------------------------- bytes ----
def cache_path(entry, env=None):
    """``<cache>/<campaign>/<sha256>-<filename>``.

    The digest makes the name prove the content; the filename is kept after it
    because every dry-run line and every SEND FAILED line in the run log prints
    this path, and a bare hex blob would make those logs unreadable."""
    return os.path.join(cache_dir(env), entry["campaign"],
                        "%s-%s" % (entry["sha256"], entry["filename"]))


def _digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url, *, opener, sleep):
    """``(body, content_type)``, one retry on a transient failure.

    Reads one byte past the cap so an oversized object is refused rather than
    held in memory in full."""
    host = urllib.parse.urlsplit(url).netloc
    for attempt in (0, 1):
        try:
            request = urllib.request.Request(
                url, method="GET", headers={"User-Agent": USER_AGENT})
            with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                body = response.read(MAX_BYTES + 1)
                kind = (response.headers.get("Content-Type") or "").split(";")[0].strip()
                return body, kind.lower()
        except urllib.error.HTTPError as ex:
            ex.close()
            if ex.code in RETRYABLE_STATUS and attempt == 0:
                sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise AssetUnusable("%s answered HTTP %d" % (host, ex.code))
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            if attempt == 0:
                sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise AssetUnusable("%s unreachable: %s" % (host, getattr(ex, "reason", ex)))
    raise AssetUnusable("%s: retries exhausted" % host)  # unreachable


def _write_bytes(path, body):
    """Atomic and durable: pid-unique temp file, fsync, rename, fsync the dir.

    Mode 0644, unlike ``operator_config._write_private_json``: this is an advert
    about to be broadcast to public groups, not a bearer token, and a readable
    mode is one less way for the gateway to fail to attach it."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def ensure(entry, env=None, *, opener=None, sleep=None, log=None):
    # Serialize cache fills with retirement.
    import campaign_deletion
    with campaign_deletion.submission(entry["campaign"], env) as active:
        if not active:
            raise AssetsUnavailable("this campaign has been deleted")
        return _ensure(entry, env, opener=opener, sleep=sleep, log=log)


def _ensure(entry, env=None, *, opener=None, sleep=None, log=None):
    """The local path for ``entry``, downloading and verifying it if needed.

    The cached file is re-hashed on every call. That is a few milliseconds
    against the cost of broadcasting a corrupt image to thirty groups, so it is
    not configurable."""
    env = os.environ if env is None else env
    opener = opener or urllib.request.urlopen
    sleep = sleep or time.sleep
    log = log or (lambda message: _log(message))
    path = cache_path(entry, env)
    if os.path.exists(path):
        try:
            if _digest(path) == entry["sha256"]:
                return path
            log("cached %s does not match its digest; fetching it again" % path)
        except OSError as ex:
            log("cached %s unreadable (%s); fetching it again" % (path, ex))
    url = public_url(entry, env)
    if url is None:
        raise AssetUnusable("%s is not set, so %s cannot be fetched"
                            % (BASE_VAR, entry["key"]))
    body, content_type = _download(url, opener=opener, sleep=sleep)
    if len(body) > MAX_BYTES:
        raise AssetUnusable("%s is larger than the %d byte limit"
                            % (entry["key"], MAX_BYTES))
    if len(body) != entry["bytes"]:
        raise AssetUnusable("%s is %d bytes, the index says %d"
                            % (entry["key"], len(body), entry["bytes"]))
    if content_type and content_type != entry["content_type"]:
        raise AssetUnusable("%s was served as %s, the index says %s"
                            % (entry["key"], content_type, entry["content_type"]))
    got = hashlib.sha256(body).hexdigest()
    if got != entry["sha256"]:
        raise AssetUnusable("%s hashes to %s, the index says %s"
                            % (entry["key"], got, entry["sha256"]))
    try:
        _write_bytes(path, body)
    except OSError as ex:
        raise AssetUnusable("could not cache %s: %s" % (entry["key"], ex))
    log("cached %s (%d bytes)" % (path, len(body)))
    return path


def prune(index_, env=None, log=None):
    """Remove cached files the index no longer references. Returns the count.

    Keys are content-addressed, so replacing an image leaves the old bytes
    behind for ever otherwise."""
    log = log or (lambda message: _log(message))
    keep = set()
    for entries in (index_.get("campaigns") or {}).values():
        for entry in entries:
            keep.add(os.path.abspath(cache_path(entry, env)))
    removed = 0
    root = cache_dir(env)
    for directory, _dirs, names in os.walk(root):
        for name in names:
            full = os.path.abspath(os.path.join(directory, name))
            if full in keep or name.endswith(".tmp"):
                continue
            try:
                os.unlink(full)
                removed += 1
            except OSError as ex:
                log("could not remove %s: %s" % (full, ex))
    if removed:
        log("removed %d cached file(s) the index no longer references" % removed)
    return removed
