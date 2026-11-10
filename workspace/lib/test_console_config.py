"""Display caching is process-shared, bounded, and bypassed before saves."""
import io
import json
import multiprocessing
import os
import sys
import time
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import console_config as cache
import operator_config as config
from test_operator_config import Harness, FakeFirestore, JID_A, creds_b64


def process_read(env, count, results):
    fake = FakeFirestore(page_size=300)
    def opener(request, **kw):
        with count.get_lock():
            count.value += 1
        time.sleep(0.02)
        return fake(request, **kw)
    try:
        result = cache.load(env, opener=opener, signer=lambda *a: "jwt",
                            now=lambda: 1_800_000_000.0, log=lambda *a: None)
        results.put(result["membership"]["groups"][JID_A]["name"])
    except Exception as exc:
        results.put(str(exc))


class ConsoleCacheTests(Harness):
    def display(self, **kw):
        return cache.load(self.env, opener=self.firestore, signer=self.signer,
                          now=lambda: self.clock[0], sleep=lambda s: None,
                          log=self.logs.append, **kw)

    def path(self):
        return os.path.join(config.state_dir(self.env), cache.CACHE_FILE)

    def test_display_reuses_config_until_expiry_but_runtime_load_is_live(self):
        first = self.display()
        requests = len(self.firestore.requests)
        self.firestore.store["groups"][JID_A]["name"] = config.encode_value("Changed")
        self.assertEqual(self.display()["membership"], first["membership"])
        self.assertEqual(len(self.firestore.requests), requests)
        self.assertEqual(self.load()["membership"]["groups"][JID_A]["name"], "Changed")
        self.clock[0] += 300
        self.assertEqual(self.display()["membership"]["groups"][JID_A]["name"], "Changed")

    def test_cold_concurrent_processes_and_a_restarted_process_share_one_read(self):
        ctx = multiprocessing.get_context("fork")
        count = ctx.Value("i", 0)
        results = ctx.Queue()
        processes = [ctx.Process(target=process_read, args=(self.env, count, results)) for _ in range(3)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual([results.get(timeout=1) for _ in processes], ["Alpha"] * 3)
        self.assertEqual(count.value, 5)  # token, settings, lines, campaigns, groups
        restarted = ctx.Process(target=process_read, args=(self.env, count, results))
        restarted.start()
        restarted.join(10)
        self.assertEqual(restarted.exitcode, 0)
        self.assertEqual(results.get(timeout=1), "Alpha")
        self.assertEqual(count.value, 5)

    def test_outage_cooldown_preserves_snapshot_timestamp_reason_and_age_limit(self):
        first = self.display()
        self.clock[0] += 300
        self.firestore.down = True
        failed = self.display()
        requests = len(self.firestore.requests)
        self.assertEqual(failed["source"], "snapshot")
        self.assertEqual(failed["fetchedAt"], first["fetchedAt"])
        self.assertIn("unreachable", failed["degraded"])
        self.clock[0] += 60
        self.assertEqual(self.display(), failed)
        self.assertEqual(len(self.firestore.requests), requests)
        self.clock[0] += config.SNAPSHOT_MAX_AGE_SECONDS
        with self.assertRaises(config.ConfigUnavailable):
            self.display()

    def test_failure_without_snapshot_is_also_throttled_and_recovery_is_detected(self):
        self.firestore.down = True
        with self.assertRaises(config.ConfigUnavailable):
            self.display()
        requests = len(self.firestore.requests)
        with self.assertRaises(config.ConfigUnavailable):
            self.display()
        self.assertEqual(len(self.firestore.requests), requests)
        self.clock[0] += 300
        self.firestore.down = False
        self.assertEqual(self.display()["source"], "firestore")

    def test_fresh_bypasses_success_and_failure_caches_and_refuses_snapshot(self):
        self.display()
        self.firestore.store["groups"][JID_A]["name"] = config.encode_value("New")
        self.assertEqual(self.display(fresh=True)["membership"]["groups"][JID_A]["name"], "New")
        self.firestore.down = True
        with self.assertRaises(config.ConfigUnavailable):
            self.display(fresh=True)
        self.assertEqual(self.display()["source"], "snapshot")
        self.firestore.down = False
        self.assertEqual(self.display(fresh=True)["source"], "firestore")

    def test_identity_changes_never_reuse_another_project_or_credentials(self):
        self.display()
        for override in ({"project_id": "other"}, {"client_email": "other@x"}, {"private_key_id": "other"}):
            self.env["GCP_SERVICE_ACCOUNT_B64"] = creds_b64(**override)
            before = len(self.firestore.requests)
            self.display()
            self.assertGreater(len(self.firestore.requests), before)

    def test_credential_identity_failure_uses_snapshot_for_display_but_not_fresh_reads(self):
        first = self.display()
        original_env = dict(self.env)
        with open(self.path(), "rb") as stream:
            cached = stream.read()
        requests = len(self.firestore.requests)
        failures = (
            {"GCP_SERVICE_ACCOUNT_B64": "malformed*credential"},
            {"GCP_SERVICE_ACCOUNT_B64": "", "GCP_SERVICE_ACCOUNT_FILE": os.path.join(self.dir, "missing-key.json")},
        )
        for override in failures:
            with self.subTest(override=tuple(override)):
                self.env = {**original_env, **override}
                result = self.display()
                self.assertEqual(result, self.load())
                self.assertEqual(result["source"], "snapshot")
                self.assertEqual(result["membership"], first["membership"])
                self.assertEqual(result["fetchedAt"], first["fetchedAt"])
                self.assertTrue(result["degraded"])
                with self.assertRaises(config.ConfigUnavailable):
                    self.display(fresh=True)
                with open(self.path(), "rb") as stream:
                    self.assertEqual(stream.read(), cached)
        self.assertEqual(len(self.firestore.requests), requests)

    def test_credential_identity_failure_cannot_serve_an_expired_snapshot(self):
        self.display()
        self.clock[0] += config.SNAPSHOT_MAX_AGE_SECONDS + 1
        self.env["GCP_SERVICE_ACCOUNT_B64"] = "malformed*credential"
        with self.assertRaisesRegex(config.ConfigUnavailable, "past the"):
            self.display()

    def test_credential_failure_filters_full_snapshot_for_scoped_displays(self):
        first = self.display()
        self.env["GCP_SERVICE_ACCOUNT_B64"] = "malformed*credential"
        result = self.display(group_ids=[JID_A])
        self.assertEqual(list(result["membership"]["groups"]), [JID_A])
        self.assertEqual(result["fetchedAt"], first["fetchedAt"])
        settings = self.display(include_groups=False, include_campaigns=False)
        self.assertEqual(settings["membership"]["groups"], {})
        self.assertEqual(settings["membership"]["promos"], {})
        with self.assertRaises(config.ConfigUnavailable):
            self.display(group_ids=[JID_A], fresh=True)

    def test_credential_identity_failure_without_a_snapshot_still_fails_closed(self):
        self.env["GCP_SERVICE_ACCOUNT_B64"] = "malformed*credential"
        with self.assertRaisesRegex(config.ConfigUnavailable, "no snapshot"):
            self.display()
        self.assertFalse(os.path.exists(self.path()))

    def test_credential_identity_failure_cannot_serve_an_invalid_snapshot(self):
        self.display()
        snapshot_path = config.snapshot_path(config.state_dir(self.env))
        with open(snapshot_path) as stream:
            snapshot = json.load(stream)
        snapshot["membership"]["version"] = 99
        with open(snapshot_path, "w") as stream:
            json.dump(snapshot, stream)
        self.env["GCP_SERVICE_ACCOUNT_B64"] = "malformed*credential"
        with self.assertRaisesRegex(config.ConfigUnavailable, "snapshot is invalid"):
            self.display()

    def test_local_and_api_invalidation_force_new_reads(self):
        self.display()
        for invalidate in (lambda: cache.invalidate(self.env),
                           lambda: self.env.update(GENAIE_CONSOLE_CONFIG_GENERATION="new-generation")):
            invalidate()
            before = len(self.firestore.requests)
            self.display()
            self.assertGreater(len(self.firestore.requests), before)

    def test_invalidation_during_read_does_not_publish_older_data(self):
        fake = self.firestore
        def opener(request, **kw):
            response = fake(request, **kw)
            if "/documents/groups?" in request.full_url:
                cache.invalidate(self.env)
            return response
        cache.load(self.env, opener=opener, signer=self.signer, now=lambda: self.clock[0])
        self.assertFalse(os.path.exists(self.path()))

    def test_corrupt_cache_or_invalid_membership_is_refreshed(self):
        self.display()
        with open(self.path()) as stream:
            entry = json.load(stream)
        for body in ("not JSON", json.dumps(dict(entry, result=dict(entry["result"], membership={"version": 99})))):
            with open(self.path(), "w") as stream:
                stream.write(body)
            before = len(self.firestore.requests)
            self.assertEqual(self.display()["membership"]["groups"][JID_A]["name"], "Alpha")
            self.assertGreater(len(self.firestore.requests), before)

    def test_a_corrupt_cached_group_is_not_returned_without_quarantine(self):
        self.display()
        with open(self.path()) as stream:
            entry = json.load(stream)
        entry["result"]["membership"]["groups"][JID_A]["status"] = "invalid"
        with open(self.path(), "w") as stream:
            json.dump(entry, stream)
        before = len(self.firestore.requests)
        self.assertEqual(self.display()["membership"]["groups"][JID_A]["status"], "active")
        self.assertGreater(len(self.firestore.requests), before)

    def test_rate_limit_metadata_survives_cached_failure_without_a_snapshot(self):
        def limited(request, **kw):
            raise urllib.error.HTTPError(request.full_url, 429, "limited", {"Retry-After": "12"}, io.BytesIO(b"{}"))
        self.firestore = limited
        # The refusal must be from Firestore rather than OAuth.
        from unittest.mock import patch
        with patch.object(config, "access_token", return_value="tok"):
            for _ in range(2):
                with self.assertRaises(config.ConfigUnavailable) as caught:
                    self.display()
                self.assertEqual(caught.exception.metadata, {"upstreamStatus": 429, "code": "config_rate_limited",
                                                            "quota": "unknown", "retryAfterSeconds": 12.0})

    def test_retirement_protection_never_reuses_a_snapshot_after_cache_invalidation(self):
        self.env["GENAIE_CAMPAIGN_DELETION"] = "1"
        self.display()
        self.firestore.down = True
        with cache.mutation(self.env):
            pass
        with self.assertRaises(config.ConfigUnavailable):
            self.display(fresh=True)
        with self.assertRaises(config.ConfigUnavailable):
            self.display()

    def test_retirement_marker_reaches_cached_readers_after_mutation(self):
        self.env["GENAIE_CAMPAIGN_DELETION"] = "1"
        self.display()
        self.firestore.store["campaigns"]["cv"] = config.encode_fields({"deleted": True, "enabled": False})
        with cache.mutation(self.env):
            pass
        result = self.display()
        self.assertIn("cv", result["deleted"])
        self.assertNotIn("cv", result["membership"]["promos"])
        self.assertFalse(result["enabled"]["cv"])

    def test_retirement_failure_preserves_quota_metadata(self):
        from unittest.mock import patch
        self.env["GENAIE_CAMPAIGN_DELETION"] = "1"
        def limited(request, **kw):
            raise urllib.error.HTTPError(request.full_url, 429, "limited", {"Retry-After": "12"}, io.BytesIO(b"{}"))
        self.firestore = limited
        with patch.object(config, "access_token", return_value="tok"):
            with self.assertRaises(config.ConfigUnavailable) as caught:
                self.display()
            self.assertEqual(caught.exception.metadata["code"], "config_rate_limited")


if __name__ == "__main__":
    unittest.main()
