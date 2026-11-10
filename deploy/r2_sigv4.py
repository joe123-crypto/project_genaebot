"""Sign one S3 request for Cloudflare R2, with nothing but the standard library.

``deploy/upload-ad-image.py`` needs exactly one authenticated operation -- PUT an
image into a bucket -- and this signs it. There is no SDK because this repo has
no Python package step at all: the container never runs ``pip install``, CI runs
the tests with a bare ``python3``, and the precedent for the one thing the
standard library cannot do is to hand-roll it (Firestore's REST calls in
``workspace/lib/operator_config.py``, RS256 in ``workspace/lib/gcp_token.mjs``).
A hand-rolled signature is also the only version whose correctness CI can
actually assert: ``deploy/test_r2_sigv4.py`` checks it against the vectors AWS
publishes, offline.

This is the easy end of SigV4 and deliberately stays there:

* one request, no multipart -- every ad image is a few megabytes against R2's
  5 GB single-PUT limit;
* the payload is in memory, so ``x-amz-content-sha256`` is a real digest and
  none of the chunked-upload or UNSIGNED-PAYLOAD machinery is needed;
* no query-string signing, no presigned URLs: the bot reads the bucket over
  plain public HTTPS and never signs anything.

R2 takes ``us-east-1`` or ``auto`` as the region and ignores it otherwise.

Upload credentials live on the operator's machine. Campaign retirement also
uses this signer for exact-key DELETEs in the console API, whose separate
bucket-scoped credentials are removed from the gateway environment by the
entrypoint. Broadcast runners continue to make only unauthenticated GETs.
"""

import datetime
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
DEFAULT_REGION = "auto"
HTTP_TIMEOUT_SECONDS = 120
# Unreserved characters stay literal in a canonical URI; S3 does not
# double-encode the path, so "/" survives and "$" becomes %24.
PATH_SAFE = "/~"


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
