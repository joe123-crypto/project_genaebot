"""Operator config read from Firestore, the canonical store.

The groups the bot advertises in, the WhatsApp lines it posts from, which
campaigns are switched on, and the cap defaults all live in Firestore and are
edited in the Firebase Console. This module reads them back into exactly the
dict ``promo_membership`` has always loaded from ``workspace/cron/membership.json``,
so nothing downstream has to know where the data came from.

Collections, one document per thing, field names identical to membership.json::

    settings/defaults   version, _comment, activity, caps_enabled,
                        contact_account, bands
    lines/<id>          number                       (defaults.accounts[id])
    campaigns/<slug>    enabled, comment, weekly_comment, dir?
                                                     (promos[slug] + the switch)
    groups/<jid>        name, source, status, activity, account, daily_cap,
                        promos, weekly, note

Every document may also carry ``rank``. Firestore lists documents by ID, but
order here is not cosmetic: a run sends in targets order and, when it runs out
of time, the groups it never reaches are the last ones. ``rank`` keeps that
order. Unranked documents sort after ranked ones, by ID.

Why the failure handling is shaped the way it is:

* **Targeting fails closed.** If Firestore cannot be read, the last-known-good
  snapshot under ``runtime/firestore/`` is used, but only while it is younger
  than :data:`SNAPSHOT_MAX_AGE_SECONDS`. With no snapshot, or one past that age,
  :func:`load` raises :class:`ConfigUnavailable` and the caller must send
  nothing. "Fail closed" has to mean closed rather than stale: a snapshot served
  indefinitely would keep advertising to a group an operator had paused. The repository's membership.json is deliberately not a fallback: once
  Firestore is canonical that file is stale, and falling back to it would bring
  old targeting back -- re-posting into a group someone had paused.
* **Validation is per document.** The Firebase Console has no schema and no CI
  in front of it. Every group is checked with ``promo_membership.validate`` on
  its own; a malformed one is quarantined (left out of sending and reported)
  without touching the other groups. A malformed ``settings``, ``lines`` or
  ``campaigns`` document affects every group, so it falls back to the snapshot.
* **The snapshot is written durably** -- pid-unique temp file, fsync, rename,
  mode 0600 -- not with ``json_store``, whose shared ``.tmp`` name lets two
  writers interleave into invalid JSON.

Transport is the Firestore REST API over ``urllib``: the image has no package
manager step, so no SDK can be installed. Signing the service-account JWT
(RS256) is the one thing the standard library cannot do; ``gcp_token.mjs`` does
only that, using Node's built-in ``node:crypto``.

Std-lib only, Python 3.11+ (the container runs Debian bookworm's python3).
"""

import base64
import datetime
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCOPE = "https://www.googleapis.com/auth/datastore"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
API_ROOT = "https://firestore.googleapis.com/v1"

# A hung Firestore must not eat a promo run's time budget: one short attempt,
# one retry, then the snapshot.
HTTP_TIMEOUT_SECONDS = 10
RETRY_BACKOFF_SECONDS = 1.0
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
# The failures a first setup actually hits, named so the log says what to fix.
HTTP_HINTS = {
    400: " (the token request was malformed -- is the key's client_email right?)",
    401: " (the token was rejected -- check the key has not been deleted)",
    403: " (permission denied -- the service account needs a Cloud Datastore role)",
    404: " (no Firestore database in this project, or the wrong project)",
}
PAGE_SIZE = 300

# How stale the last-known-good snapshot may be before it stops counting as
# config at all. Long enough to ride out an outage that spans a day's runs,
# short enough that a paused group cannot be advertised for a second day.
SNAPSHOT_MAX_AGE_SECONDS = 48 * 3600

TOKEN_LIFETIME_SECONDS = 3600
# Mint a fresh token this long before the cached one expires, so a request is
# never sent with a token that dies in flight.
TOKEN_REFRESH_SLACK_SECONDS = 120

COLLECTIONS = ("settings", "lines", "campaigns", "groups")
SETTINGS_DOC = "defaults"
# membership.json keeps these at the top level, not under ``defaults``.
TOP_LEVEL_SETTINGS = ("_comment", "version")
RANK = "rank"
SIGNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gcp_token.mjs")

DEFAULT_STATE_DIR = "/home/joseph/.openclaw"


class ConfigUnavailable(Exception):
    """No usable config: Firestore unreachable or refusing, and no snapshot."""


class ConfigInvalid(Exception):
    """Firestore answered, but a document every group depends on is malformed."""


def state_dir(env=None):
    """``runtime/firestore`` on the volume -- the tier that survives restart and
    redeploy. ``workspace/`` would not: it is wiped on every boot."""
    env = os.environ if env is None else env
    override = env.get("GENAIE_FIRESTORE_DIR")
    if override:
        return override
    root = env.get("OPENCLAW_STATE_DIR") or DEFAULT_STATE_DIR
    return os.path.join(root, "runtime", "firestore")


# --------------------------------------------------------------------------
# Credentials and the access token


def credentials(env=None):
    """The service-account key as a dict, or ``None`` when none is configured.

    Production carries it base64-encoded in ``GCP_SERVICE_ACCOUNT_B64``: the
    key's ``private_key`` holds literal newlines, and the entrypoint rewrites
    whitespace and quotes in the values it copies into ``.env``. A local checkout
    or the seed script may point ``GCP_SERVICE_ACCOUNT_FILE`` at the JSON file."""
    env = os.environ if env is None else env
    # Whitespace is dropped before decoding: `base64` wraps at 76 columns by
    # default, and a pasted value can pick up a trailing newline.
    encoded = "".join((env.get("GCP_SERVICE_ACCOUNT_B64") or "").split())
    if encoded:
        try:
            raw = base64.b64decode(encoded, validate=True)
            creds = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # Deliberately no detail: the payload is a private key.
            raise ConfigUnavailable("GCP_SERVICE_ACCOUNT_B64 is not base64-encoded JSON")
    else:
        path = (env.get("GCP_SERVICE_ACCOUNT_FILE") or "").strip()
        if not path:
            return None
        try:
            with open(path, encoding="utf-8") as f:
                creds = json.load(f)
        except (OSError, ValueError) as ex:
            raise ConfigUnavailable("cannot read GCP_SERVICE_ACCOUNT_FILE %s: %s"
                                    % (path, type(ex).__name__))
    missing = [key for key in ("client_email", "private_key", "project_id")
               if not isinstance(creds.get(key), str) or not creds.get(key)]
    if missing:
        raise ConfigUnavailable("service-account key is missing %s" % ", ".join(missing))
    return creds


def node_signer(header, claims, private_key):
    """Sign with ``gcp_token.mjs``. The key travels over stdin, never argv."""
    try:
        run = subprocess.run(
            ["node", SIGNER],
            input=json.dumps({"header": header, "claims": claims,
                              "private_key": private_key}).encode("utf-8"),
            capture_output=True, timeout=HTTP_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise ConfigUnavailable("node is not on PATH; it signs the service-account JWT")
    except subprocess.TimeoutExpired:
        raise ConfigUnavailable("JWT signer timed out")
    if run.returncode != 0:
        # gcp_token.mjs never writes key material to stderr.
        raise ConfigUnavailable("JWT signer failed: %s"
                                % run.stderr.decode("utf-8", "replace").strip()[:300])
    return run.stdout.decode("ascii").strip()


def access_token(creds, cache_path, *, opener, signer, now, sleep):
    """A bearer token for Firestore, reusing the cached one until it is close to
    expiring. Ten promo runs a day should mint one token an hour, not one each."""
    cached = _read_json(cache_path)
    if (isinstance(cached, dict)
            and cached.get("client_email") == creds["client_email"]
            and isinstance(cached.get("expires_at"), (int, float))
            and cached["expires_at"] - TOKEN_REFRESH_SLACK_SECONDS > now()
            and isinstance(cached.get("access_token"), str)):
        return cached["access_token"]

    issued = int(now())
    header = {"alg": "RS256", "typ": "JWT"}
    if creds.get("private_key_id"):
        header["kid"] = creds["private_key_id"]
    claims = {"iss": creds["client_email"], "scope": SCOPE, "aud": TOKEN_URL,
              "iat": issued, "exp": issued + TOKEN_LIFETIME_SECONDS}
    assertion = signer(header, claims, creds["private_key"])
    body = urllib.parse.urlencode({"grant_type": GRANT_TYPE,
                                   "assertion": assertion}).encode("ascii")
    reply = _request(TOKEN_URL, opener=opener, sleep=sleep, data=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    token = reply.get("access_token")
    if not isinstance(token, str) or not token:
        raise ConfigUnavailable("token endpoint returned no access_token")
    lifetime = reply.get("expires_in")
    if not isinstance(lifetime, int) or isinstance(lifetime, bool):
        lifetime = TOKEN_LIFETIME_SECONDS
    try:
        _write_private_json(cache_path, {"client_email": creds["client_email"],
                                         "access_token": token,
                                         "expires_at": issued + lifetime})
    except OSError as ex:
        # A cache that cannot be written costs one extra mint next run, never a send.
        _log("could not cache the access token: %s" % ex)
    return token


# --------------------------------------------------------------------------
# HTTP


def _request(url, *, opener, sleep, data=None, headers=None, method=None,
             missing_ok=False):
    """JSON from ``url``, one retry on a transient failure. Raises
    :class:`ConfigUnavailable` with the host and status only -- never a token,
    and never the request body, which for the token call is a signed assertion."""
    host = urllib.parse.urlsplit(url).netloc
    for attempt in (0, 1):
        request = urllib.request.Request(url, data=data, headers=dict(headers or {}),
                                         method=method)
        try:
            with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as ex:
            # An HTTPError holds the response; close it now rather than at GC.
            ex.close()
            if missing_ok and ex.code == 404:
                return None
            if ex.code in RETRYABLE_STATUS and attempt == 0:
                sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise ConfigUnavailable("%s answered HTTP %d%s"
                                    % (host, ex.code, HTTP_HINTS.get(ex.code, "")))
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            if attempt == 0:
                sleep(RETRY_BACKOFF_SECONDS)
                continue
            reason = getattr(ex, "reason", ex)
            raise ConfigUnavailable("%s unreachable: %s" % (host, reason))
        except ValueError:
            raise ConfigUnavailable("%s returned a body that is not JSON" % host)
    raise ConfigUnavailable("%s: retries exhausted" % host)  # unreachable


def _auth(token):
    return {"Authorization": "Bearer %s" % token}


def documents_url(project, collection, doc_id=None):
    url = "%s/projects/%s/databases/(default)/documents/%s" % (
        API_ROOT, urllib.parse.quote(project, safe=""), urllib.parse.quote(collection, safe=""))
    if doc_id is not None:
        # A JID carries "@"; quote everything so no ID can change the path.
        url += "/" + urllib.parse.quote(doc_id, safe="")
    return url


def doc_id_of(name):
    """The document ID from a REST resource name (``.../documents/groups/<id>``)."""
    return urllib.parse.unquote(name.rsplit("/", 1)[-1])


def list_documents(project, collection, token, *, opener, sleep):
    """``[(doc_id, raw_fields)]`` for every document in ``collection``."""
    out = []
    page = None
    while True:
        query = {"pageSize": PAGE_SIZE}
        if page:
            query["pageToken"] = page
        reply = _request("%s?%s" % (documents_url(project, collection),
                                    urllib.parse.urlencode(query)),
                         opener=opener, sleep=sleep, headers=_auth(token))
        for doc in reply.get("documents") or []:
            out.append((doc_id_of(doc["name"]), doc.get("fields") or {}))
        page = reply.get("nextPageToken")
        if not page:
            return out


def get_document(project, collection, doc_id, token, *, opener, sleep):
    """One document's decoded fields, or ``None`` if it does not exist. The send
    loop re-reads a group with this just before posting to it."""
    reply = _request(documents_url(project, collection, doc_id), opener=opener,
                     sleep=sleep, headers=_auth(token), missing_ok=True)
    if reply is None:
        return None
    return decode_fields(reply.get("fields") or {})


# --------------------------------------------------------------------------
# Firestore's typed values


def decode_value(value):
    """A Firestore REST value object -- exactly one typed key -- as plain JSON.
    ``integerValue`` arrives as a string (int64 does not fit a JSON number), and
    an empty array or map arrives with no ``values``/``fields`` key at all."""
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError("not a Firestore value: %r" % (value,))
    kind, payload = next(iter(value.items()))
    if kind == "nullValue":
        return None
    if kind == "booleanValue":
        return bool(payload)
    if kind == "integerValue":
        return int(payload)
    if kind == "doubleValue":
        return float(payload)
    if kind == "stringValue":
        return payload
    if kind == "arrayValue":
        return [decode_value(item) for item in (payload or {}).get("values") or []]
    if kind == "mapValue":
        return decode_fields((payload or {}).get("fields") or {})
    # Timestamps, references, bytes and geo points have no place in this
    # config; one appearing means someone picked the wrong type in the Console.
    raise ValueError("unsupported Firestore type %s" % kind)


def decode_fields(fields):
    return dict((name, decode_value(value)) for name, value in fields.items())


def encode_value(value, _in_array=False):
    """The inverse of :func:`decode_value`. ``bool`` is checked before ``int``
    because in Python a bool *is* an int, and ``True`` must not become ``1``."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        if _in_array:
            raise ValueError("Firestore cannot store an array directly inside an array")
        return {"arrayValue": {"values": [encode_value(item, True) for item in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": encode_fields(value)}}
    raise ValueError("cannot store %s in Firestore" % type(value).__name__)


def encode_fields(data):
    return dict((str(name), encode_value(value)) for name, value in data.items())


# --------------------------------------------------------------------------
# Assembly: four collections back into one membership dict


def _ranked(rows):
    def key(row):
        rank = row[1].get(RANK)
        ranked = isinstance(rank, int) and not isinstance(rank, bool)
        return (0, rank, row[0]) if ranked else (1, 0, row[0])
    return sorted(rows, key=key)


def _without(data, *names):
    return dict((k, v) for k, v in data.items() if k not in names)


def assemble(raw):
    """``(membership, enabled, undecodable)`` from ``{collection: [(id, raw_fields)]}``.

    ``membership`` has the exact shape of membership.json. ``enabled`` maps each
    campaign to whether it is switched on. ``undecodable`` maps a group to why its
    document could not even be read (a timestamp where a string belongs, say), so
    it is quarantined like any other malformed group rather than failing the lot.
    A settings, line or campaign document that cannot be read affects every
    group, so that raises :class:`ConfigInvalid`."""
    decoded = {}
    undecodable = {}
    for collection in COLLECTIONS:
        rows = []
        for doc_id, fields in raw.get(collection) or []:
            try:
                rows.append((doc_id, decode_fields(fields)))
            except ValueError as ex:
                if collection == "groups":
                    undecodable[doc_id] = ["group %s: %s" % (doc_id, ex)]
                else:
                    raise ConfigInvalid("%s/%s: %s" % (collection, doc_id, ex))
        decoded[collection] = _ranked(rows)

    settings = dict(decoded["settings"]).get(SETTINGS_DOC)
    if settings is None:
        raise ConfigInvalid("settings/%s is missing" % SETTINGS_DOC)

    membership = {}
    for name in TOP_LEVEL_SETTINGS:
        if name in settings:
            membership[name] = settings[name]
    defaults = _without(settings, RANK, *TOP_LEVEL_SETTINGS)
    defaults["accounts"] = dict((line_id, _without(data, RANK))
                                for line_id, data in decoded["lines"])
    membership["defaults"] = defaults
    membership["promos"] = dict((slug, _without(data, RANK, "enabled"))
                                for slug, data in decoded["campaigns"])
    membership["groups"] = dict((jid, _without(data, RANK))
                                for jid, data in decoded["groups"])
    enabled = dict((slug, data.get("enabled", True)) for slug, data in decoded["campaigns"])
    return membership, enabled, undecodable


def quarantine(membership, undecodable=None):
    """``(clean, report)``. Each group is validated on its own, against the shared
    settings; one that fails is left out of ``clean`` and listed in
    ``report["quarantined"]`` with its problems. Problems in the shared settings
    raise :class:`ConfigInvalid`. Problems that only show across groups (two
    groups with the same name) are reported as warnings and quarantine nothing --
    they matter to the operator, not to whether a post is safe."""
    import promo_membership  # deferred: promo_membership will import this module

    shared = dict(membership)
    shared["groups"] = {}
    problems = promo_membership.validate(shared)
    if problems:
        raise ConfigInvalid("; ".join(problems))

    declared = set(membership.get("promos") or {})
    quarantined = dict(undecodable or {})
    dropped = []
    kept = {}
    for jid, entry in (membership.get("groups") or {}).items():
        entry, gone = _drop_unknown_promos(entry, declared)
        if gone:
            dropped.append("group %s: dropped campaign(s) %s, which no campaign "
                           "document declares" % (jid, ", ".join(gone)))
        probe = dict(shared)
        probe["groups"] = {jid: entry}
        errors = promo_membership.validate(probe)
        if errors:
            quarantined[jid] = errors
        else:
            kept[jid] = entry
    clean = dict(membership)
    clean["groups"] = kept
    return clean, {"quarantined": quarantined,
                   "warnings": dropped + promo_membership.validate(clean)}


def _drop_unknown_promos(entry, declared):
    """``(entry, dropped)`` with references to undeclared campaigns removed.

    Deleting a campaign document must not quarantine every group that still
    lists it: for ``cv`` that would silently stop all advertising to 45 groups
    over one stale reference. A campaign that does not exist cannot be sent
    either way, so dropping the reference is safe in both directions and the
    group keeps its other campaigns. Contrast an unknown *line*, which does
    quarantine -- posting from the wrong number is not safe."""
    if not isinstance(entry, dict):
        return entry, []
    promos = entry.get("promos")
    weekly = entry.get("weekly")
    gone = []
    if isinstance(promos, list):
        gone = [p for p in promos if isinstance(p, str) and p not in declared]
    if isinstance(weekly, dict):
        gone += [p for p in weekly if p not in declared and p not in gone]
    if not gone:
        return entry, []
    entry = dict(entry)
    if isinstance(promos, list):
        entry["promos"] = [p for p in promos if p not in gone]
    if isinstance(weekly, dict):
        entry["weekly"] = dict((p, days) for p, days in weekly.items() if p not in gone)
    return entry, gone


def campaign_switches(enabled):
    """``(effective, problems)``. ``enabled`` must be a boolean, as everywhere else
    in this repo; anything else ("true" typed as a string) counts as off and is
    reported. Off is the safe reading of an ambiguous kill switch."""
    effective = {}
    problems = {}
    for slug, value in enabled.items():
        if isinstance(value, bool):
            effective[slug] = value
        else:
            effective[slug] = False
            problems[slug] = "enabled must be true or false, got %r" % (value,)
    return effective, problems


# --------------------------------------------------------------------------
# The snapshot and the change log


def snapshot_path(directory):
    return os.path.join(directory, "operator-config.json")


def token_cache_path(directory):
    return os.path.join(directory, "token.json")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_private_json(path, payload):
    """Atomic and durable: a pid-unique temp file, fsync, rename, fsync the
    directory. Mode 0600 because the token cache holds a bearer token."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
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


def _by_document(membership, enabled):
    """Flatten to ``{(collection, id): fields}`` so two configs can be diffed
    document by document, the way they are edited."""
    docs = {}
    settings = _without((membership or {}).get("defaults") or {}, "accounts")
    for name in TOP_LEVEL_SETTINGS:
        if name in (membership or {}):
            settings[name] = membership[name]
    docs[("settings", SETTINGS_DOC)] = settings
    for line_id, data in (((membership or {}).get("defaults") or {}).get("accounts") or {}).items():
        docs[("lines", line_id)] = data
    for slug, data in ((membership or {}).get("promos") or {}).items():
        doc = dict(data)
        if slug in (enabled or {}):
            doc["enabled"] = enabled[slug]
        docs[("campaigns", slug)] = doc
    for jid, data in ((membership or {}).get("groups") or {}).items():
        docs[("groups", jid)] = data
    return docs



def manifest_switches(manifest):
    """``{slug: bool}`` from deploy/promo-crons.json, read the way the rest of
    the repo reads it: an absent ``enabled`` means on, a non-boolean is an error."""
    out = {}
    for row in (manifest or {}).get("promos") or []:
        value = row.get("enabled", True)
        if not isinstance(value, bool):
            raise ConfigInvalid("promo-crons.json: %s has non-boolean enabled=%r"
                                % (row.get("slug"), value))
        out[row["slug"]] = value
    return out


def parity_diff(expected, expected_switches, got, got_switches):
    """Human-readable differences; empty means the copy is exact. Content is
    compared field by field, then order (which decides who a short run reaches),
    then the generated targets for every campaign -- the payload the send path
    actually consumes."""
    import promo_membership  # deferred: promo_membership will import this module

    problems = []
    canon = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False)  # noqa: E731
    exp_docs = _by_document(expected, expected_switches)
    got_docs = _by_document(got, got_switches)
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

def changes(before, after, at):
    """What changed between two snapshots, one entry per document. Firestore
    keeps no history of Console edits; this is the record of what changed and
    when (not who -- the read side cannot know)."""
    old = _by_document(before.get("membership"), before.get("enabled"))
    new = _by_document(after.get("membership"), after.get("enabled"))
    out = []
    for key in sorted(set(old) | set(new)):
        collection, doc_id = key
        if key not in old:
            out.append({"at": at, "change": "added", "collection": collection, "id": doc_id})
        elif key not in new:
            out.append({"at": at, "change": "removed", "collection": collection, "id": doc_id})
        elif old[key] != new[key]:
            fields = sorted(name for name in set(old[key]) | set(new[key])
                            if old[key].get(name) != new[key].get(name))
            out.append({"at": at, "change": "modified", "collection": collection,
                        "id": doc_id, "fields": fields})
    return out


def _append_changes(directory, entries, today):
    if not entries:
        return
    path = os.path.join(directory, "changes-%s.jsonl" % today.strftime("%Y-%m"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# The entry point


def _log(message):
    sys.stderr.write("[operator-config] %s\n" % message)


def fetch(env=None, *, opener=None, signer=None, now=None, sleep=None):
    """Read all four collections live. Raises :class:`ConfigUnavailable` when
    Firestore cannot be reached or refuses. Returns ``(project, raw)``."""
    env = os.environ if env is None else env
    opener = opener or urllib.request.urlopen
    signer = signer or node_signer
    now = now or time.time
    sleep = sleep or time.sleep
    creds = credentials(env)
    if creds is None:
        raise ConfigUnavailable("no service account configured "
                                "(GCP_SERVICE_ACCOUNT_B64 or GCP_SERVICE_ACCOUNT_FILE)")
    token = access_token(creds, token_cache_path(state_dir(env)), opener=opener,
                         signer=signer, now=now, sleep=sleep)
    raw = dict((collection, list_documents(creds["project_id"], collection, token,
                                           opener=opener, sleep=sleep))
               for collection in COLLECTIONS)
    return creds["project_id"], raw


def load(env=None, *, opener=None, signer=None, now=None, sleep=None, log=None):
    """The operator config to act on, as a dict::

        {"source": "firestore" | "snapshot",
         "membership": <membership.json-shaped dict, quarantined groups removed>,
         "enabled": {slug: bool},          # campaign switches, typos read as off
         "report": {"quarantined": {jid: [problems]}, "warnings": [...],
                    "campaigns": {slug: problem}},
         "fetchedAt": ISO-8601 UTC, "degraded": None | why Firestore was not used}

    Raises :class:`ConfigUnavailable` when neither Firestore nor a snapshot is
    usable. A caller deciding where to post must then post nowhere."""
    env = os.environ if env is None else env
    now = now or time.time
    log = log or _log
    directory = state_dir(env)
    try:
        _, raw = fetch(env, opener=opener, signer=signer, now=now, sleep=sleep)
        membership, enabled, undecodable = assemble(raw)
        clean, report = quarantine(membership, undecodable)
    except (ConfigUnavailable, ConfigInvalid) as ex:
        log("Firestore unusable (%s); using the last-known-good snapshot" % ex)
        return _from_snapshot(directory, str(ex), now=now)

    at = datetime.datetime.fromtimestamp(now(), datetime.timezone.utc)
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    current = {"fetchedAt": stamp, "membership": membership, "enabled": enabled}
    try:
        previous = _read_json(snapshot_path(directory))
        # Snapshot first, change log second: if the log append fails we lose a
        # line of history; the other order would re-log the same change next run.
        _write_private_json(snapshot_path(directory), current)
        if isinstance(previous, dict):
            _append_changes(directory, changes(previous, current, stamp), at)
    except OSError as ex:
        # The live read already succeeded; failing to keep a copy degrades the
        # next outage, not this run.
        log("could not write the snapshot: %s" % ex)

    switches, campaign_problems = campaign_switches(enabled)
    report["campaigns"] = campaign_problems
    _report(report, log)
    return {"source": "firestore", "membership": clean, "enabled": switches,
            "report": report, "fetchedAt": stamp, "degraded": None}


def _snapshot_age_seconds(snapshot, now):
    """How old the snapshot is, or ``None`` when it does not say. A snapshot
    written by :func:`load` always carries ``fetchedAt``."""
    stamp = snapshot.get("fetchedAt")
    if not isinstance(stamp, str):
        return None
    try:
        at = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return now() - at.timestamp()


def _from_snapshot(directory, why, now=None):
    now = now or time.time
    snapshot = _read_json(snapshot_path(directory))
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("membership"), dict)
            or not isinstance(snapshot.get("enabled"), dict)):
        raise ConfigUnavailable("Firestore unusable (%s) and no snapshot at %s"
                                % (why, snapshot_path(directory)))
    # Yesterday's targeting through a short outage is fine. Last week's is not:
    # it is how a group an operator paused gets advertised again, which is the
    # one failure this whole path exists to prevent. Past the bound there is no
    # trustworthy config, and the caller must send nothing.
    age = _snapshot_age_seconds(snapshot, now)
    if age is None:
        raise ConfigUnavailable("Firestore unusable (%s) and the snapshot at %s does "
                                "not say when it was taken"
                                % (why, snapshot_path(directory)))
    if age > SNAPSHOT_MAX_AGE_SECONDS:
        raise ConfigUnavailable(
            "Firestore unusable (%s) and the snapshot is %.0f h old, past the %.0f h "
            "limit; refusing to act on stale targeting"
            % (why, age / 3600.0, SNAPSHOT_MAX_AGE_SECONDS / 3600.0))
    try:
        clean, report = quarantine(snapshot["membership"])
    except ConfigInvalid as ex:
        raise ConfigUnavailable("Firestore unusable (%s) and the snapshot is invalid (%s)"
                                % (why, ex))
    switches, campaign_problems = campaign_switches(snapshot["enabled"])
    report["campaigns"] = campaign_problems
    return {"source": "snapshot", "membership": clean, "enabled": switches,
            "report": report, "fetchedAt": snapshot.get("fetchedAt"), "degraded": why}


def _report(report, log):
    for jid, problems in sorted(report["quarantined"].items()):
        log("quarantined group %s: %s" % (jid, "; ".join(problems)))
    for slug, problem in sorted(report.get("campaigns", {}).items()):
        log("campaign %s treated as off: %s" % (slug, problem))
    for warning in report["warnings"]:
        log("warning: %s" % warning)
