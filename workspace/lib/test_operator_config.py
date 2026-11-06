"""Cover operator_config: the value codec, assembly and order, per-group
quarantine, the fail-closed fallback ladder, token caching, and that no key or
token ever reaches a log line.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them. Fully offline: Firestore and Google's token endpoint
are an in-memory fake behind the same ``opener`` seam urllib uses, because the
acceptance job runs with ``--network none``.
"""

import base64
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import operator_config as oc  # noqa: E402

PROJECT = "genaie-test"
CANARY_KEY = "-----BEGIN PRIVATE KEY-----\nCANARY-KEY-MATERIAL\n-----END PRIVATE KEY-----\n"
JID_A = "120363000000000001@g.us"
JID_B = "120363000000000002@g.us"
JID_C = "120363000000000003@g.us"


def creds_b64(**overrides):
    creds = {"type": "service_account", "project_id": PROJECT,
             "client_email": "runtime@%s.iam.gserviceaccount.com" % PROJECT,
             "private_key": CANARY_KEY, "private_key_id": "kid-1"}
    creds.update(overrides)
    return base64.b64encode(json.dumps(creds).encode()).decode()


def read(path):
    with open(path) as f:
        return json.load(f)


def membership():
    """A small membership in the exact shape of the real file."""
    return {
        "_comment": "test",
        "version": 1,
        "defaults": {"activity": "normal", "caps_enabled": True,
                     "contact_account": "default",
                     "accounts": {"default": {"number": "+10000000001"},
                                  "ads": {"number": "+10000000002"}},
                     "bands": {"busy": [6, 10], "normal": [4, 6], "quiet": [1, 3]}},
        "promos": {"genaie": {"comment": "g"},
                   "cv": {"comment": "c", "weekly_comment": "w"}},
        "groups": {
            JID_B: {"name": "Bravo", "source": "operator", "status": "active",
                    "daily_cap": None, "promos": ["genaie", "cv"],
                    "weekly": {"cv": ["mon", "thu"]}},
            JID_A: {"name": "Alpha", "source": "whatsapp", "status": "active",
                    "daily_cap": 3, "promos": ["genaie"], "account": "ads"},
            JID_C: {"name": "Charlie", "status": "on-hold", "daily_cap": None,
                    "promos": [], "note": "do NOT re-add from the discovery sweep"},
        },
    }


def seed_store(m=None, enabled=None):
    """``{collection: {doc_id: encoded_fields}}`` the way the seed writes it:
    file order carried in ``rank``."""
    m = m or membership()
    enabled = enabled or {"genaie": True, "cv": False}
    settings = dict((k, m[k]) for k in ("_comment", "version") if k in m)
    settings.update((k, v) for k, v in m["defaults"].items() if k != "accounts")
    store = {"settings": {"defaults": oc.encode_fields(settings)}, "lines": {},
             "campaigns": {}, "groups": {}}
    for i, (line, data) in enumerate(m["defaults"]["accounts"].items()):
        store["lines"][line] = oc.encode_fields(dict(data, rank=i * 10))
    for i, (slug, data) in enumerate(m["promos"].items()):
        store["campaigns"][slug] = oc.encode_fields(dict(data, enabled=enabled[slug],
                                                         rank=i * 10))
    for i, (jid, data) in enumerate(m["groups"].items()):
        store["groups"][jid] = oc.encode_fields(dict(data, rank=i * 10))
    return store


class Response(object):
    def __init__(self, payload):
        self._body = io.BytesIO(json.dumps(payload).encode())

    def read(self):
        return self._body.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeFirestore(object):
    """Google's token endpoint and Firestore's list/get, in memory. Lists by
    document ID, as Firestore does, and pages, so order and pagination are both
    exercised."""

    def __init__(self, store=None, page_size=2):
        self.store = store if store is not None else seed_store()
        self.page_size = page_size
        self.down = False
        self.fail_next = []          # HTTP codes to answer with, one per request
        self.token_status = None     # e.g. 403 for a key without a role
        self.mints = 0
        self.requests = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(url)
        if self.down:
            raise urllib.error.URLError("connection refused")
        if self.fail_next:
            raise urllib.error.HTTPError(url, self.fail_next.pop(0), "fail", {}, io.BytesIO())
        if url == oc.TOKEN_URL:
            if self.token_status:
                raise urllib.error.HTTPError(url, self.token_status, "no", {}, io.BytesIO())
            self.mints += 1
            return Response({"access_token": "tok-%d" % self.mints, "expires_in": 3600,
                             "token_type": "Bearer"})
        assert request.get_header("Authorization", "").startswith("Bearer tok-"), url
        parts = urllib.parse.urlsplit(url)
        tail = parts.path.split("/documents/", 1)[1].split("/")
        collection = urllib.parse.unquote(tail[0])
        docs = self.store.get(collection, {})
        if len(tail) == 2:
            doc_id = urllib.parse.unquote(tail[1])
            if doc_id not in docs:
                raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO())
            return Response({"name": "x/documents/%s/%s" % (collection, doc_id),
                             "fields": docs[doc_id]})
        query = urllib.parse.parse_qs(parts.query)
        start = int((query.get("pageToken") or ["0"])[0])
        ids = sorted(docs)
        page = ids[start:start + self.page_size]
        reply = {"documents": [{"name": "projects/%s/databases/(default)/documents/%s/%s"
                                        % (PROJECT, collection, urllib.parse.quote(i)),
                                "fields": docs[i]} for i in page]}
        if start + self.page_size < len(ids):
            reply["nextPageToken"] = str(start + self.page_size)
        return Response(reply)


class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {"GENAIE_FIRESTORE_DIR": os.path.join(self.dir, "fs"),
                    "GCP_SERVICE_ACCOUNT_B64": creds_b64()}
        self.firestore = FakeFirestore()
        self.signed = []
        self.clock = [1_800_000_000.0]
        self.logs = []

    def signer(self, header, claims, key):
        self.signed.append((header, claims))
        return "jwt-%d" % len(self.signed)

    def load(self, **kw):
        return oc.load(self.env, opener=self.firestore, signer=self.signer,
                       now=lambda: self.clock[0], sleep=lambda s: None,
                       log=self.logs.append, **kw)


class CodecTest(unittest.TestCase):
    def roundtrip(self, value):
        return oc.decode_value(oc.encode_value(value))

    def test_every_shape_the_config_uses_survives_a_round_trip(self):
        value = {"n": None, "t": True, "f": False, "i": 0, "big": 2 ** 62, "s": "x",
                 "list": ["mon", "thu"], "empty_list": [], "empty_map": {},
                 "bands": {"busy": [6, 10]}, "nested": {"a": {"b": [1]}}}
        self.assertEqual(self.roundtrip(value), value)

    def test_a_boolean_never_turns_into_a_number(self):
        self.assertEqual(oc.encode_value(True), {"booleanValue": True})
        self.assertIs(self.roundtrip(True), True)
        self.assertIs(self.roundtrip(False), False)

    def test_integers_travel_as_strings_the_way_firestore_sends_them(self):
        self.assertEqual(oc.encode_value(7), {"integerValue": "7"})
        self.assertEqual(oc.decode_value({"integerValue": "7"}), 7)

    def test_an_empty_array_or_map_arrives_without_its_inner_key(self):
        self.assertEqual(oc.decode_value({"arrayValue": {}}), [])
        self.assertEqual(oc.decode_value({"mapValue": {}}), {})

    def test_firestore_forbids_an_array_inside_an_array(self):
        with self.assertRaises(ValueError):
            oc.encode_value([[1, 2]])

    def test_types_with_no_place_in_config_are_refused(self):
        with self.assertRaises(ValueError):
            oc.decode_value({"timestampValue": "2026-11-04T00:00:00Z"})
        with self.assertRaises(ValueError):
            oc.decode_value({"stringValue": "a", "integerValue": "1"})


class AssemblyTest(Harness):
    def test_the_collections_reassemble_into_the_exact_membership(self):
        result = self.load()
        self.assertEqual(result["source"], "firestore")
        self.assertEqual(json.dumps(result["membership"], sort_keys=True),
                         json.dumps(membership(), sort_keys=True))

    def test_rank_restores_file_order_although_firestore_lists_by_id(self):
        # The fake lists by ID (A, B, C); the file order is B, A, C.
        got = self.load()["membership"]
        self.assertEqual(list(got["groups"]), [JID_B, JID_A, JID_C])
        self.assertEqual(list(got["defaults"]["accounts"]), ["default", "ads"])
        self.assertEqual(list(got["promos"]), ["genaie", "cv"])

    def test_an_unranked_group_added_in_the_console_goes_last(self):
        self.firestore.store["groups"]["100000000000000000@g.us"] = oc.encode_fields(
            {"name": "New", "status": "active", "promos": ["genaie"]})
        order = list(self.load()["membership"]["groups"])
        self.assertEqual(order[-1], "100000000000000000@g.us")

    def test_rank_and_the_switch_never_leak_into_the_membership_shape(self):
        got = self.load()["membership"]
        self.assertNotIn("rank", got["groups"][JID_A])
        self.assertNotIn("rank", got["defaults"]["accounts"]["ads"])
        self.assertNotIn("enabled", got["promos"]["cv"])

    def test_campaign_switches_are_read_with_house_semantics(self):
        self.assertEqual(self.load()["enabled"], {"genaie": True, "cv": False})

    def test_a_switch_typed_as_text_counts_as_off_and_is_reported(self):
        self.firestore.store["campaigns"]["genaie"]["enabled"] = {"stringValue": "true"}
        result = self.load()
        self.assertIs(result["enabled"]["genaie"], False)
        self.assertIn("genaie", result["report"]["campaigns"])

    def test_pagination_is_followed_to_the_end(self):
        self.firestore.page_size = 1
        self.assertEqual(len(self.load()["membership"]["groups"]), 3)


class QuarantineTest(Harness):
    def test_one_malformed_group_is_held_back_and_the_rest_are_untouched(self):
        self.firestore.store["groups"][JID_A]["status"] = {"stringValue": "Active"}
        result = self.load()
        self.assertNotIn(JID_A, result["membership"]["groups"])
        self.assertEqual(set(result["membership"]["groups"]), {JID_B, JID_C})
        self.assertIn(JID_A, result["report"]["quarantined"])
        self.assertTrue(any("quarantined group %s" % JID_A in line for line in self.logs))

    def test_a_group_on_an_undeclared_line_is_held_back(self):
        # Posting from the wrong number is not safe, so this one quarantines.
        self.firestore.store["groups"][JID_A]["account"] = {"stringValue": "ad"}
        self.assertIn(JID_A, self.load()["report"]["quarantined"])

    def test_a_group_whose_document_cannot_even_be_decoded_is_held_back(self):
        self.firestore.store["groups"][JID_A]["name"] = {"timestampValue": "2026-11-04T00:00:00Z"}
        result = self.load()
        self.assertIn(JID_A, result["report"]["quarantined"])
        self.assertIn(JID_B, result["membership"]["groups"])

    def test_deleting_a_campaign_drops_the_reference_instead_of_the_group(self):
        del self.firestore.store["campaigns"]["cv"]
        result = self.load()
        bravo = result["membership"]["groups"][JID_B]
        self.assertEqual(bravo["promos"], ["genaie"])
        self.assertEqual(bravo["weekly"], {})
        self.assertEqual(result["report"]["quarantined"], {})
        self.assertTrue(any("dropped campaign(s) cv" in w for w in result["report"]["warnings"]))

    def test_duplicate_names_are_a_warning_not_a_quarantine(self):
        self.firestore.store["groups"][JID_A]["name"] = {"stringValue": "Bravo"}
        result = self.load()
        self.assertEqual(result["report"]["quarantined"], {})
        self.assertTrue(result["report"]["warnings"])


class FallbackTest(Harness):
    def test_a_live_read_leaves_a_private_snapshot_behind(self):
        self.load()
        path = oc.snapshot_path(self.env["GENAIE_FIRESTORE_DIR"])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(read(path)["membership"]["groups"][JID_A]["name"], "Alpha")

    def test_an_outage_serves_the_last_known_good_snapshot(self):
        self.load()
        self.firestore.down = True
        result = self.load()
        self.assertEqual(result["source"], "snapshot")
        self.assertIn("unreachable", result["degraded"])
        self.assertEqual(len(result["membership"]["groups"]), 3)

    def test_an_outage_with_no_snapshot_means_send_nowhere(self):
        self.firestore.down = True
        with self.assertRaises(oc.ConfigUnavailable):
            self.load()

    def test_no_credentials_with_no_snapshot_also_means_send_nowhere(self):
        del self.env["GCP_SERVICE_ACCOUNT_B64"]
        with self.assertRaises(oc.ConfigUnavailable):
            self.load()

    def test_broken_shared_settings_fall_back_rather_than_quarantine_everything(self):
        self.load()
        self.firestore.store["settings"]["defaults"]["contact_account"] = {"stringValue": "nobody"}
        result = self.load()
        self.assertEqual(result["source"], "snapshot")

    def test_a_missing_settings_document_falls_back(self):
        self.load()
        del self.firestore.store["settings"]["defaults"]
        self.assertEqual(self.load()["source"], "snapshot")

    def test_a_snapshot_that_no_longer_validates_is_not_trusted(self):
        self.load()
        path = oc.snapshot_path(self.env["GENAIE_FIRESTORE_DIR"])
        snap = read(path)
        snap["membership"]["version"] = 99
        with open(path, "w") as f:
            json.dump(snap, f)
        self.firestore.down = True
        with self.assertRaises(oc.ConfigUnavailable):
            self.load()

    def test_a_snapshot_still_inside_the_age_limit_is_served(self):
        self.load()
        self.firestore.down = True
        self.clock[0] += oc.SNAPSHOT_MAX_AGE_SECONDS - 60
        self.assertEqual(self.load()["source"], "snapshot")

    def test_a_snapshot_past_the_age_limit_means_send_nowhere(self):
        # Fail closed has to mean closed, not stale: serving a week-old snapshot
        # would keep advertising to a group the operator paused days ago.
        self.load()
        self.firestore.down = True
        self.clock[0] += oc.SNAPSHOT_MAX_AGE_SECONDS + 60
        with self.assertRaises(oc.ConfigUnavailable) as caught:
            self.load()
        self.assertIn("past the", str(caught.exception))

    def test_a_snapshot_that_does_not_say_when_it_was_taken_is_not_trusted(self):
        self.load()
        path = oc.snapshot_path(self.env["GENAIE_FIRESTORE_DIR"])
        snap = read(path)
        del snap["fetchedAt"]
        with open(path, "w") as f:
            json.dump(snap, f)
        self.firestore.down = True
        with self.assertRaises(oc.ConfigUnavailable):
            self.load()

    def test_a_live_read_refreshes_the_age(self):
        self.load()
        self.clock[0] += oc.SNAPSHOT_MAX_AGE_SECONDS - 60
        self.load()                      # live, so the snapshot is rewritten
        self.clock[0] += 120             # past the limit measured from the FIRST read
        self.firestore.down = True
        self.assertEqual(self.load()["source"], "snapshot")

    def test_a_transient_failure_is_retried_once(self):
        self.firestore.fail_next = [503]
        self.assertEqual(self.load()["source"], "firestore")

    def test_the_baked_membership_file_is_never_a_fallback(self):
        # Even with a membership.json sitting right there in the repo, an outage
        # with no snapshot must not quietly act on it.
        self.firestore.down = True
        with self.assertRaises(oc.ConfigUnavailable):
            self.load()
        self.assertFalse(any("membership.json" in line for line in self.logs))


class TokenTest(Harness):
    def test_consecutive_runs_mint_one_token(self):
        self.load()
        self.load()
        self.assertEqual(len(self.signed), 1)
        self.assertEqual(self.firestore.mints, 1)

    def test_a_token_close_to_expiry_is_replaced(self):
        self.load()
        self.clock[0] += 3600 - oc.TOKEN_REFRESH_SLACK_SECONDS + 1
        self.load()
        self.assertEqual(len(self.signed), 2)

    def test_a_different_key_does_not_reuse_another_accounts_token(self):
        self.load()
        self.env["GCP_SERVICE_ACCOUNT_B64"] = creds_b64(client_email="other@x.iam.gserviceaccount.com")
        self.load()
        self.assertEqual(len(self.signed), 2)

    def test_the_jwt_asks_for_exactly_the_datastore_scope_for_an_hour(self):
        self.load()
        header, claims = self.signed[0]
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT", "kid": "kid-1"})
        self.assertEqual(claims["scope"], oc.SCOPE)
        self.assertEqual(claims["aud"], oc.TOKEN_URL)
        self.assertEqual(claims["exp"] - claims["iat"], 3600)

    def test_the_token_cache_is_private(self):
        self.load()
        path = oc.token_cache_path(self.env["GENAIE_FIRESTORE_DIR"])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_wrapped_base64_key_still_decodes(self):
        encoded = creds_b64()
        self.env["GCP_SERVICE_ACCOUNT_B64"] = "\n".join(
            encoded[i:i + 76] for i in range(0, len(encoded), 76)) + "\n"
        self.assertEqual(self.load()["source"], "firestore")


class SecretsTest(Harness):
    def test_no_key_or_token_reaches_a_log_line_or_an_error(self):
        self.firestore.token_status = 403
        try:
            self.load()
        except oc.ConfigUnavailable as ex:
            self.logs.append(str(ex))
        text = "\n".join(self.logs)
        self.assertIn("permission denied", text)
        self.assertNotIn("CANARY-KEY-MATERIAL", text)
        self.assertNotIn("jwt-", text)

    def test_a_bad_base64_key_is_refused_without_quoting_it(self):
        self.env["GCP_SERVICE_ACCOUNT_B64"] = "not*base64*CANARY"
        with self.assertRaises(oc.ConfigUnavailable) as caught:
            self.load()
        self.assertNotIn("CANARY", str(caught.exception))


class ChangeLogTest(Harness):
    def test_an_edit_in_the_console_is_recorded_with_the_field_that_changed(self):
        self.load()
        self.firestore.store["groups"][JID_A]["status"] = {"stringValue": "on-hold"}
        self.clock[0] += 60
        self.load()
        directory = self.env["GENAIE_FIRESTORE_DIR"]
        logs = [name for name in os.listdir(directory) if name.startswith("changes-")]
        self.assertEqual(len(logs), 1)
        with open(os.path.join(directory, logs[0])) as f:
            entries = [json.loads(line) for line in f]
        self.assertEqual(entries, [{"at": entries[0]["at"], "change": "modified",
                                    "collection": "groups", "id": JID_A,
                                    "fields": ["status"]}])

    def test_an_unchanged_read_records_nothing(self):
        self.load()
        self.load()
        directory = self.env["GENAIE_FIRESTORE_DIR"]
        self.assertFalse([n for n in os.listdir(directory) if n.startswith("changes-")])


class DocumentTest(Harness):
    def test_a_single_group_is_fetched_by_its_quoted_jid(self):
        got = oc.get_document(PROJECT, "groups", JID_A, "tok-1",
                              opener=self.firestore, sleep=lambda s: None)
        self.assertEqual(got["name"], "Alpha")
        self.assertIn("%40g.us", self.firestore.requests[-1])

    def test_a_deleted_group_reads_as_none(self):
        self.assertIsNone(oc.get_document(PROJECT, "groups", "gone@g.us", "tok-1",
                                          opener=self.firestore, sleep=lambda s: None))


class NodeSignerTest(unittest.TestCase):
    """The real signer, end to end through its stdin path. Skipped only where no
    node binary exists; CI and the image both have one."""

    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node is not installed")

    def test_the_python_side_gets_back_a_jwt_that_verifies(self):
        keys = json.loads(subprocess.run(
            ["node", "-e", "const c=require('node:crypto');const k=c.generateKeyPairSync('rsa',"
             "{modulusLength:2048,publicKeyEncoding:{type:'spki',format:'pem'},"
             "privateKeyEncoding:{type:'pkcs8',format:'pem'}});console.log(JSON.stringify(k))"],
            capture_output=True, check=True).stdout)
        jwt = oc.node_signer({"alg": "RS256", "typ": "JWT"}, {"iss": "x"}, keys["privateKey"])
        check = subprocess.run(
            ["node", "-e", "const c=require('node:crypto');const [h,p,s]=process.argv[1].split('.');"
             "process.exit(c.verify('sha256',Buffer.from(h+'.'+p),process.argv[2],"
             "Buffer.from(s,'base64url'))?0:1)", jwt, keys["publicKey"]])
        self.assertEqual(check.returncode, 0)

    def test_a_signer_failure_never_quotes_the_key(self):
        with self.assertRaises(oc.ConfigUnavailable) as caught:
            oc.node_signer({"alg": "RS256", "typ": "JWT"}, {}, CANARY_KEY)
        self.assertNotIn("CANARY-KEY-MATERIAL", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
