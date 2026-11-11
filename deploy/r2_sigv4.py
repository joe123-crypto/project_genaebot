"""Sign one S3 request for Cloudflare R2, with nothing but the standard library.

``deploy/upload-ad-image.py`` needs one authenticated operation -- PUT an image
into a bucket -- and ``deploy/backup-operator-data.py`` needs one more, listing
the bucket, and this signs both. There is no SDK because this repo has
no Python package step at all: the container never runs ``pip install``, CI runs
the tests with a bare ``python3``, and the precedent for the one thing the
standard library cannot do is to hand-roll it (Firestore's REST calls in
``workspace/lib/operator_config.py``, RS256 in ``workspace/lib/gcp_token.mjs``).
A hand-rolled signature is also the only version whose correctness CI can
actually assert: ``deploy/test_r2_sigv4.py`` checks it against the vectors AWS
publishes, offline.

This is the easy end of SigV4 and deliberately stays there:

* no multipart -- every ad image is a few megabytes against R2's 5 GB
  single-PUT limit;
* the payload is in memory (empty, for a listing), so
  ``x-amz-content-sha256`` is a real digest and none of the chunked-upload or
  UNSIGNED-PAYLOAD machinery is needed;
* no presigned URLs: the bot reads the bucket over plain public HTTPS and never
  signs anything. A listing's parameters are signed in the canonical query
  string like any other header, not in the URL an operator could share.

The bucket credentials themselves are read here too, from the operator's
``~/.config/genaie/r2-upload.env``, so the uploader and the backup tool cannot
disagree about where they come from or refuse a world-readable file differently.

R2 takes ``us-east-1`` or ``auto`` as the region and ignores it otherwise.

Upload credentials live on the operator's machine. Campaign retirement also
uses this signer for exact-key DELETEs in the console API, whose separate
bucket-scoped credentials are removed from the gateway environment by the
entrypoint. Broadcast runners continue to make only unauthenticated GETs.
"""

import datetime
import hashlib
import hmac
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
DEFAULT_REGION = "auto"
HTTP_TIMEOUT_SECONDS = 120
# Unreserved characters stay literal in a canonical URI; S3 does not
# double-encode the path, so "/" survives and "$" becomes %24.
PATH_SAFE = "/~"
# ListObjectsV2 caps a page at 1000 keys whatever is asked for.
LIST_PAGE_SIZE = 1000
# S3 list replies are namespaced; every element below is read through this.
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
DEFAULT_ENV_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "genaie", "r2-upload.env")


class R2Error(Exception):
    """The bucket refused the request, or could not be reached."""


def _sha256(body):
    return hashlib.sha256(body).hexdigest()


def _hmac(key, message):
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key, datestamp, region, service=SERVICE):
    """The date/region/service-scoped key AWS derives before signing anything."""
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def _canonical_headers(headers):
    """``(canonical_headers, signed_headers)``: lowercase names, trimmed values,
    sorted. Every header that goes on the wire must be signed, or the service
    recomputes a different signature and rejects it."""
    pairs = sorted((name.lower(), " ".join(str(value).split()))
                   for name, value in headers.items())
    return ("".join("%s:%s\n" % pair for pair in pairs),
            ";".join(name for name, _ in pairs))


def canonical_request(method, url, headers, payload_sha256):
    """The canonical request, exactly as the signature is computed over it."""
    split = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(split.path or "/", safe=PATH_SAFE)
    query = "&".join(
        "%s=%s" % (urllib.parse.quote(k, safe="~"), urllib.parse.quote(v, safe="~"))
        for k, v in sorted(urllib.parse.parse_qsl(split.query, keep_blank_values=True)))
    canonical, signed = _canonical_headers(headers)
    return ("\n".join([method, path, query, canonical, signed, payload_sha256]), signed)


def signed_headers(*, method, url, region, access_key, secret_key, payload_sha256,
                   headers=None, now=None, service=SERVICE):
    """``headers`` plus ``Authorization``, ready to send.

    ``headers`` must already contain every header that will go on the wire,
    ``host``, ``x-amz-date`` and ``x-amz-content-sha256`` included -- the caller
    owns them so a published test vector can be reproduced byte for byte."""
    headers = dict(headers or {})
    amzdate = headers.get("x-amz-date")
    if not amzdate:
        at = now() if now else datetime.datetime.now(datetime.timezone.utc)
        amzdate = at.strftime("%Y%m%dT%H%M%SZ")
        headers["x-amz-date"] = amzdate
    datestamp = amzdate[:8]
    scope = "%s/%s/%s/aws4_request" % (datestamp, region, service)
    canonical, signed = canonical_request(method, url, headers, payload_sha256)
    to_sign = "\n".join([ALGORITHM, amzdate, scope, _sha256(canonical.encode("utf-8"))])
    signature = hmac.new(signing_key(secret_key, datestamp, region, service),
                         to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        "%s Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (ALGORITHM, access_key, scope, signed, signature))
    return headers


def object_url(endpoint, bucket, key):
    return "%s/%s/%s" % (endpoint.rstrip("/"), urllib.parse.quote(bucket, safe="~"),
                         urllib.parse.quote(key, safe=PATH_SAFE))


def put_object(endpoint, bucket, key, body, content_type, *, access_key, secret_key,
               region=DEFAULT_REGION, opener=None, now=None):
    """PUT ``body`` at ``key``. Returns the object's URL; raises :class:`R2Error`.

    No retry: the caller is an operator watching the output, and a half-written
    object is better reported than silently re-attempted."""
    opener = opener or urllib.request.urlopen
    url = object_url(endpoint, bucket, key)
    digest = _sha256(body)
    headers = signed_headers(
        method="PUT", url=url, region=region, access_key=access_key,
        secret_key=secret_key, payload_sha256=digest, now=now,
        headers={"host": urllib.parse.urlsplit(url).netloc,
                 "content-type": content_type,
                 "content-length": str(len(body)),
                 "x-amz-content-sha256": digest})
    request = urllib.request.Request(url, data=body, headers=headers, method="PUT")
    try:
        with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status not in (200, 201):
                raise R2Error("%s answered HTTP %d" % (bucket, response.status))
    except urllib.error.HTTPError as ex:
        detail = ""
        try:
            detail = (ex.read() or b"").decode("utf-8", "replace")[:400]
        except Exception:
            pass
        ex.close()
        raise R2Error("%s refused the upload: HTTP %d %s" % (bucket, ex.code, detail))
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        raise R2Error("%s unreachable: %s" % (bucket, getattr(ex, "reason", ex)))
    return url


def _get(url, *, region, access_key, secret_key, opener, now):
    """A signed GET of ``url``, returning the body. Empty payload, real digest."""
    digest = _sha256(b"")
    headers = signed_headers(
        method="GET", url=url, region=region, access_key=access_key,
        secret_key=secret_key, payload_sha256=digest, now=now,
        headers={"host": urllib.parse.urlsplit(url).netloc,
                 "x-amz-content-sha256": digest})
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise R2Error("%s answered HTTP %d" % (url, response.status))
            return response.read()
    except urllib.error.HTTPError as ex:
        detail = ""
        try:
            detail = (ex.read() or b"").decode("utf-8", "replace")[:400]
        except Exception:
            pass
        ex.close()
        raise R2Error("%s refused the request: HTTP %d %s" % (url, ex.code, detail))
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        raise R2Error("%s unreachable: %s" % (url, getattr(ex, "reason", ex)))


def _text(element, name):
    found = element.find(S3_NS + name)
    return found.text if found is not None and found.text is not None else ""


def list_objects(endpoint, bucket, *, access_key, secret_key, prefix=None,
                 region=DEFAULT_REGION, opener=None, now=None):
    """Every object in ``bucket`` as ``[{"key", "bytes", "etag", "modified"}]``.

    ListObjectsV2, followed page by page, sorted by key so two listings of an
    unchanged bucket compare equal. The etag of a single-PUT object is its MD5,
    which is not what the asset index records, so it is kept only as R2's own
    opaque version marker -- the digest that matters is the sha256 in the index.
    """
    opener = opener or urllib.request.urlopen
    base = "%s/%s" % (endpoint.rstrip("/"), urllib.parse.quote(bucket, safe="~"))
    out = []
    token = None
    while True:
        query = {"list-type": "2", "max-keys": str(LIST_PAGE_SIZE)}
        if prefix:
            query["prefix"] = prefix
        if token:
            query["continuation-token"] = token
        body = _get("%s?%s" % (base, urllib.parse.urlencode(query)), region=region,
                    access_key=access_key, secret_key=secret_key, opener=opener, now=now)
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as ex:
            raise R2Error("%s returned an unreadable listing: %s" % (bucket, ex))
        for item in root.findall(S3_NS + "Contents"):
            size = _text(item, "Size")
            out.append({"key": _text(item, "Key"),
                        "bytes": int(size) if size.isdigit() else None,
                        "etag": _text(item, "ETag").strip('"'),
                        "modified": _text(item, "LastModified")})
        if _text(root, "IsTruncated") != "true":
            return sorted(out, key=lambda row: row["key"])
        token = _text(root, "NextContinuationToken")
        if not token:
            # Truncated with nowhere to continue: report it rather than return
            # a listing that silently stops short of the bucket's contents.
            raise R2Error("%s said the listing was truncated but gave no "
                          "continuation token" % bucket)


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
            raise R2Error("line %d is not KEY=VALUE: %r" % (number, raw.strip()))
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
            raise R2Error("no such env file: %s" % path)
        return None
    except OSError as ex:
        raise R2Error("cannot read %s: %s" % (path, ex))
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        raise R2Error("%s is readable by other accounts; run "
                      "`chmod 600 %s` and try again" % (path, path))
    try:
        values = parse_env_file(text)
    except R2Error as ex:
        raise R2Error("%s: %s" % (path, ex))
    for name, value in values.items():
        env.setdefault(name, value)
    return path


def settings(env=None, *, endpoint=None, bucket=None):
    """``(endpoint, bucket, access_key, secret_key)`` from the environment.

    An explicit argument wins over the variable, as a command-line flag should,
    and ``R2_ACCOUNT_ID`` is expanded into the account's S3 endpoint."""
    env = os.environ if env is None else env
    endpoint = endpoint or env.get("R2_ENDPOINT")
    account = env.get("R2_ACCOUNT_ID")
    if not endpoint and account:
        endpoint = "https://%s.r2.cloudflarestorage.com" % account
    bucket = bucket or env.get("R2_BUCKET")
    access = env.get("R2_ACCESS_KEY_ID")
    secret = env.get("R2_SECRET_ACCESS_KEY")
    missing = [name for name, value in (("R2_ACCOUNT_ID or --endpoint", endpoint),
                                        ("R2_BUCKET or --bucket", bucket),
                                        ("R2_ACCESS_KEY_ID", access),
                                        ("R2_SECRET_ACCESS_KEY", secret)) if not value]
    if missing:
        raise R2Error("R2 is not configured: no %s" % ", ".join(missing))
    return endpoint, bucket, access, secret


def target(endpoint, bucket):
    """``<host>/<bucket>`` for the log, with the account id shortened.

    Enough for an operator to see they are pointed at the right bucket in the
    right account; not the whole account id, because this line ends up in
    terminal scrollback and pasted output."""
    host = urllib.parse.urlsplit(endpoint).netloc
    account, _, rest = host.partition(".")
    if len(account) > 8:
        host = "%s...%s.%s" % (account[:6], account[-2:], rest)
    return "%s/%s" % (host, bucket)
