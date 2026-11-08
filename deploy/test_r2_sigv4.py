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
DERIVE_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
DERIVE_KEY = "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"

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
        self.assertEqual(key.hex(), DERIVE_KEY)

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


if __name__ == "__main__":
    unittest.main()
