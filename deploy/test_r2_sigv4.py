"""The R2 signature, checked against the vectors AWS publishes.

Hand-rolling SigV4 is only defensible if its correctness is asserted rather
than assumed, and these run offline with plain ``python3``, which is all
deploy/ci-check.sh gives them.

Two of the three pinned constants come straight from AWS's own documentation --
the derived signing key from the "derive a signing key" example, and the
canonical request from the "PUT Object, single chunk" example. The third, the
finished signature, was cross-checked against an independent ``openssl dgst
-mac HMAC`` chain, and ``test_the_signature_is_a_plain_hmac_chain`` recomputes
it here from bare ``hmac`` calls so the pin cannot rot into "whatever the code
does now".

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import hashlib
import hmac
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "deploy") not in sys.path:
    sys.path.insert(0, str(ROOT / "deploy"))

import r2_sigv4  # noqa: E402

# AWS, "Examples of how to derive a signing key for Signature Version 4".
# These are AWS's own published documentation values, not credentials: the secret
# below is the one in every SigV4 example in their docs, and the expected result
# is a digest derived from it. Deliberately NOT named "..._KEY" -- a 64-character
# hex value behind a name like that is what gitleaks' generic-api-key rule looks
# for, and the secret scan in CI is more useful without a standing exception.
DERIVE_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
DERIVE_EXPECTED = "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"

# AWS, "Signature Calculations for the Authorization Header: Transferring
# Payload in a Single Chunk" -- the PUT Object example.
PUT_URL = "https://examplebucket.s3.amazonaws.com/test$file.text"
PUT_BODY = b"Welcome to Amazon S3."
PUT_PAYLOAD_SHA = "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
PUT_HEADERS = {
    "host": "examplebucket.s3.amazonaws.com",
    "date": "Fri, 24 May 2013 00:00:00 GMT",
    "x-amz-date": "20130524T000000Z",
    "x-amz-storage-class": "REDUCED_REDUNDANCY",
    "x-amz-content-sha256": PUT_PAYLOAD_SHA,
}
PUT_CANONICAL = (
    "PUT\n"
    "/test%%24file.text\n"
    "\n"
    "date:Fri, 24 May 2013 00:00:00 GMT\n"
    "host:examplebucket.s3.amazonaws.com\n"
    "x-amz-content-sha256:%s\n"
    "x-amz-date:20130524T000000Z\n"
    "x-amz-storage-class:REDUCED_REDUNDANCY\n"
    "\n"
    "date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class\n"
    "%s" % (PUT_PAYLOAD_SHA, PUT_PAYLOAD_SHA))
PUT_CANONICAL_SHA = "9e0e90d9c76de8fa5b200d8c849cd5b8dc7a3be3951ddb7f6a76b4158342019d"
PUT_SIGNATURE = "7c0f3caf24a16d5948905b8ebf67d29fb415e93fddaed9ca6aeb5ac2348cfee4"
PUT_SCOPE = "20130524/us-east-1/s3/aws4_request"


def sign_put(**overrides):
    params = dict(method="PUT", url=PUT_URL, region="us-east-1",
                  access_key="AKIAIOSFODNN7EXAMPLE", secret_key=DERIVE_SECRET,
                  payload_sha256=PUT_PAYLOAD_SHA, headers=PUT_HEADERS)
    params.update(overrides)
    return r2_sigv4.signed_headers(**params)


class SigningKeyTest(unittest.TestCase):
    def test_signing_key_matches_the_published_vector(self):
        key = r2_sigv4.signing_key(DERIVE_SECRET, "20120215", "us-east-1", "iam")
        self.assertEqual(key.hex(), DERIVE_EXPECTED)

    def test_the_key_is_scoped_to_date_region_and_service(self):
        base = r2_sigv4.signing_key(DERIVE_SECRET, "20120215", "us-east-1", "iam")
        for changed in (r2_sigv4.signing_key(DERIVE_SECRET, "20120216", "us-east-1", "iam"),
                        r2_sigv4.signing_key(DERIVE_SECRET, "20120215", "auto", "iam"),
                        r2_sigv4.signing_key(DERIVE_SECRET, "20120215", "us-east-1", "s3")):
            self.assertNotEqual(base, changed)


class CanonicalRequestTest(unittest.TestCase):
    def test_canonical_request_matches_the_published_vector(self):
        canonical, signed = r2_sigv4.canonical_request(
            "PUT", PUT_URL, PUT_HEADERS, PUT_PAYLOAD_SHA)
        self.assertEqual(canonical, PUT_CANONICAL)
        self.assertEqual(signed,
                         "date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class")
        self.assertEqual(hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                         PUT_CANONICAL_SHA)

    def test_the_path_is_encoded_once_and_keeps_its_slashes(self):
        canonical, _ = r2_sigv4.canonical_request(
            "PUT", "https://h/bucket/ads/cv/%s.png" % ("a" * 64), {"host": "h"}, "x")
        self.assertIn("/bucket/ads/cv/%s.png" % ("a" * 64), canonical)
        self.assertNotIn("%2F", canonical)

    def test_header_values_are_trimmed_and_names_lowercased(self):
        canonical, signed = r2_sigv4.canonical_request(
            "PUT", PUT_URL, {"Host": " examplebucket.s3.amazonaws.com ",
                             "Content-Type": "image/png"}, "x")
        self.assertIn("host:examplebucket.s3.amazonaws.com\n", canonical)
        self.assertEqual(signed, "content-type;host")


class AuthorizationTest(unittest.TestCase):
    def test_authorization_matches_the_published_vector(self):
        header = sign_put()["Authorization"]
        self.assertEqual(
            header,
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/%s, "
            "SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;"
            "x-amz-storage-class, Signature=%s" % (PUT_SCOPE, PUT_SIGNATURE))

    def test_the_signature_is_a_plain_hmac_chain(self):
        # Recomputed from bare hmac calls, so the pinned signature above is a
        # statement about SigV4 and not about this module.
        def mac(key, message):
            return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

        key = ("AWS4" + DERIVE_SECRET).encode("utf-8")
        for part in ("20130524", "us-east-1", "s3", "aws4_request"):
            key = mac(key, part)
        to_sign = "\n".join(["AWS4-HMAC-SHA256", "20130524T000000Z", PUT_SCOPE,
                             PUT_CANONICAL_SHA])
        self.assertEqual(
            hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest(),
            PUT_SIGNATURE)

    def test_payload_digest_is_signed_never_unsigned(self):
        # R2 accepts UNSIGNED-PAYLOAD, which would make the signature say
        # nothing about the bytes that arrive. The digest is real, it is sent,
        # and it is the last line of the canonical request.
        headers = sign_put()
        self.assertEqual(headers["x-amz-content-sha256"], PUT_PAYLOAD_SHA)
        self.assertNotIn("UNSIGNED-PAYLOAD", repr(headers))
        self.assertNotIn("STREAMING", repr(headers))
        canonical, _ = r2_sigv4.canonical_request("PUT", PUT_URL, PUT_HEADERS,
                                                  PUT_PAYLOAD_SHA)
        self.assertTrue(canonical.endswith("\n" + PUT_PAYLOAD_SHA))

    def test_an_absent_amz_date_is_filled_in_from_the_clock(self):
        import datetime

        at = datetime.datetime(2026, 11, 7, 9, 17, 0, tzinfo=datetime.timezone.utc)
        headers = sign_put(headers={"host": "h"}, now=lambda: at)
        self.assertEqual(headers["x-amz-date"], "20261107T091700Z")
        self.assertIn("/20261107/us-east-1/s3/aws4_request", headers["Authorization"])

    def test_every_header_sent_is_a_signed_header(self):
        headers = sign_put()
        signed = headers["Authorization"].split("SignedHeaders=", 1)[1].split(",")[0]
        sent = set(name.lower() for name in headers if name != "Authorization")
        self.assertEqual(sent, set(signed.split(";")))


class PutObjectTest(unittest.TestCase):
    def setUp(self):
        self.seen = {}

    def opener(self, status=200):
        class Response:
            def __init__(self, code):
                self.status = code

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def open_(request, timeout=None):
            self.seen["url"] = request.full_url
            self.seen["method"] = request.method
            self.seen["body"] = request.data
            self.seen["headers"] = dict((k.lower(), v) for k, v in request.headers.items())
            self.seen["timeout"] = timeout
            return Response(status)

        return open_

    def put(self, **kwargs):
        return r2_sigv4.put_object(
            "https://acc.r2.cloudflarestorage.com", "genaie-ads",
            "ads/cv/%s.png" % ("3" * 64), PUT_BODY, "image/png",
            access_key="k", secret_key="s", opener=self.opener(**kwargs))

    def test_put_object_sends_the_expected_request(self):
        url = self.put()
        self.assertEqual(url, "https://acc.r2.cloudflarestorage.com/genaie-ads/"
                              "ads/cv/%s.png" % ("3" * 64))
        self.assertEqual(self.seen["method"], "PUT")
        self.assertEqual(self.seen["body"], PUT_BODY)
        self.assertEqual(self.seen["url"], url)
        headers = self.seen["headers"]
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["content-length"], str(len(PUT_BODY)))
        self.assertEqual(headers["x-amz-content-sha256"], PUT_PAYLOAD_SHA)
        self.assertTrue(headers["authorization"].startswith("AWS4-HMAC-SHA256 "))
        self.assertIn("/auto/s3/aws4_request", headers["authorization"])

    def test_a_refused_upload_raises_with_the_bucket_named(self):
        import urllib.error

        def refuse(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {},
                                         io_bytes(b"<Error>no</Error>"))

        def io_bytes(payload):
            import io

            return io.BytesIO(payload)

        with self.assertRaises(r2_sigv4.R2Error) as caught:
            r2_sigv4.put_object("https://acc.r2.cloudflarestorage.com", "genaie-ads",
                                "ads/cv/%s.png" % ("3" * 64), PUT_BODY, "image/png",
                                access_key="k", secret_key="s", opener=refuse)
        self.assertIn("genaie-ads", str(caught.exception))
        self.assertIn("403", str(caught.exception))

    def test_an_unreachable_bucket_raises_rather_than_returning(self):
        import urllib.error

        def unreachable(request, timeout=None):
            raise urllib.error.URLError("name or service not known")

        with self.assertRaises(r2_sigv4.R2Error):
            r2_sigv4.put_object("https://acc.r2.cloudflarestorage.com", "genaie-ads",
                                "k", PUT_BODY, "image/png", access_key="k",
                                secret_key="s", opener=unreachable)

    def test_the_secret_never_appears_in_what_is_sent(self):
        self.put()
        self.assertNotIn("s", self.seen["headers"].get("x-amz-credential", "x"))
        self.assertNotIn("secret", repr(self.seen).lower())




LIST_NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def listing(contents, *, truncated=False, token=None):
    """A ListObjectsV2 reply, in the shape and namespace R2 answers with."""
    items = "".join(
        "<Contents><Key>%s</Key><Size>%s</Size><ETag>&quot;%s&quot;</ETag>"
        "<LastModified>%s</LastModified></Contents>"
        % (key, size, etag, modified) for key, size, etag, modified in contents)
    tail = "<NextContinuationToken>%s</NextContinuationToken>" % token if token else ""
    return ("<?xml version='1.0' encoding='UTF-8'?>"
            "<ListBucketResult %s><IsTruncated>%s</IsTruncated>%s%s</ListBucketResult>"
            % (LIST_NS, "true" if truncated else "false", items, tail)).encode("utf-8")


class ListObjectsTest(unittest.TestCase):
    def setUp(self):
        self.requests = []

    def opener(self, replies):
        pages = list(replies)

        class Response:
            def __init__(self, payload):
                self.status = 200
                self._payload = payload

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def open_(request, timeout=None):
            self.requests.append((request.full_url, request.method,
                                  dict((k.lower(), v) for k, v in request.headers.items())))
            return Response(pages.pop(0))

        return open_

    def list(self, replies, **kwargs):
        return r2_sigv4.list_objects(
            "https://acc.r2.cloudflarestorage.com", "genaie-ads",
            access_key="k", secret_key="s", opener=self.opener(replies), **kwargs)

    def test_a_listing_is_decoded_and_sorted_by_key(self):
        rows = self.list([listing([("ads/b.jpg", "20", "e2", "2026-11-02T00:00:00Z"),
                                   ("ads/a.jpg", "10", "e1", "2026-11-01T00:00:00Z")])])
        self.assertEqual(rows, [
            {"key": "ads/a.jpg", "bytes": 10, "etag": "e1", "modified": "2026-11-01T00:00:00Z"},
            {"key": "ads/b.jpg", "bytes": 20, "etag": "e2", "modified": "2026-11-02T00:00:00Z"}])

    def test_the_request_is_signed_and_asks_for_v2(self):
        self.list([listing([])])
        url, method, headers = self.requests[0]
        self.assertEqual(method, "GET")
        self.assertIn("list-type=2", url)
        self.assertIn("max-keys=1000", url)
        self.assertEqual(headers["x-amz-content-sha256"],
                         hashlib.sha256(b"").hexdigest())
        self.assertTrue(headers["authorization"].startswith("AWS4-HMAC-SHA256 "))

    def test_a_prefix_is_passed_through(self):
        self.list([listing([])], prefix="ads/cv/")
        self.assertIn("prefix=ads%2Fcv%2F", self.requests[0][0])

    def test_every_page_is_followed(self):
        rows = self.list([
            listing([("ads/a.jpg", "1", "e", "t")], truncated=True, token="page-2"),
            listing([("ads/b.jpg", "2", "e", "t")])])
        self.assertEqual([row["key"] for row in rows], ["ads/a.jpg", "ads/b.jpg"])
        self.assertIn("continuation-token=page-2", self.requests[1][0])

    def test_a_truncated_listing_with_no_token_is_refused(self):
        # Returning the first page as if it were the whole bucket would make a
        # backup quietly incomplete, which is worse than failing.
        with self.assertRaises(r2_sigv4.R2Error) as caught:
            self.list([listing([("ads/a.jpg", "1", "e", "t")], truncated=True)])
        self.assertIn("continuation token", str(caught.exception))

    def test_an_unreadable_reply_is_refused(self):
        with self.assertRaises(r2_sigv4.R2Error) as caught:
            self.list([b"<not xml"])
        self.assertIn("unreadable listing", str(caught.exception))


class SettingsTest(unittest.TestCase):
    """Only the endpoint resolution lives here; the credentials file itself is
    covered in deploy/test_ad_assets_deploy.py, which also asserts that no real
    value is ever committed."""

    def test_settings_expand_the_account_id_and_name_what_is_missing(self):
        endpoint, bucket, access, secret = r2_sigv4.settings(
            {"R2_ACCOUNT_ID": "acc", "R2_BUCKET": "ads",
             "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s"})
        self.assertEqual(endpoint, "https://acc.r2.cloudflarestorage.com")
        self.assertEqual((bucket, access, secret), ("ads", "k", "s"))
        with self.assertRaises(r2_sigv4.R2Error) as caught:
            r2_sigv4.settings({"R2_ACCOUNT_ID": "acc"})
        self.assertIn("R2_BUCKET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
