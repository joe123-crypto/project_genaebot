from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


RELEASE = module("release", ROOT / "deploy/railway-release.py")
READY = module("ready", ROOT / "deploy/railway/report-release-ready.py")
SHA = "a" * 40


class ReleaseTests(unittest.TestCase):
    def test_preserve_and_explicit_profiles_reach_the_release_and_readiness_check(self):
        for requested, current, expected in [("preserve", "1", "1"),
                                             ("preserve", "0", "0"),
                                             ("preserve", None, "0"),
                                             ("baseline", "1", "0"),
                                             ("catalog-trial", "0", "1")]:
            with self.subTest(requested=requested, current=current), tempfile.TemporaryDirectory() as temp:
                calls = []
                def archive(source, sha, destination):
                    (Path(destination) / "deploy").mkdir()
                    (Path(destination) / "deploy/promo-crons.json").write_text('{"promos":[{"name":"fixture-promo"}]}')
                def railway(*args, **kwargs):
                    calls.append(args)
                    if args[:2] == ("variable", "list"):
                        return {} if current is None else {"GENAIE_CATALOG_IDLE_RETIREMENT": current}
                    if args[0] == "up": return {"deploymentId": "new"}
                    return {}
                previous = {"id": "old", "status": "SUCCESS", "meta": {"imageDigest": "old"}}
                report = {}
                with patch.object(RELEASE.subprocess, "check_output", side_effect=[SHA+"\n", SHA+"\trefs/heads/main\n"]), \
                     patch.object(RELEASE, "deployments", return_value=[previous]), \
                     patch.object(RELEASE, "archive_commit", side_effect=archive), \
                     patch.object(RELEASE, "railway", side_effect=railway), \
                     patch.object(RELEASE, "wait_for", return_value={"meta": {"imageDigest": "new"}}), \
                     patch.object(RELEASE, "startup_result", return_value={"ready": True}) as startup:
                    RELEASE.release(Path(temp), SHA, requested, report)
                setting = next(args for args in calls if args[:2] == ("variable", "set"))
                self.assertIn(f"GENAIE_CATALOG_IDLE_RETIREMENT={expected}", setting)
                startup.assert_called_once_with("new", SHA, expected, {"fixture-promo"})
                self.assertEqual(report["requestedProfile"], requested)
                self.assertEqual(report["profile"], "catalog-trial" if expected == "1" else "baseline")
                self.assertEqual(report["status"], "healthy")

    def test_unknown_preserved_setting_fails_before_mutating_production(self):
        with patch.object(RELEASE.subprocess, "check_output", side_effect=[SHA+"\n", SHA+"\trefs/heads/main\n"]), \
             patch.object(RELEASE, "deployments", return_value=[{
                 "id": "old", "status": "SUCCESS", "meta": {"imageDigest": "old"}}]), \
             patch.object(RELEASE, "railway", return_value={"GENAIE_CATALOG_IDLE_RETIREMENT": "unknown"}) as railway:
            with self.assertRaisesRegex(ValueError, "unrecognized"):
                RELEASE.release(ROOT, SHA, "preserve", {})
        self.assertEqual(railway.call_count, 1)
        self.assertEqual(railway.call_args.args[:2], ("variable", "list"))

    def report(self):
        return {"ready": True, "deploymentId": "new", "sourceCommit": SHA,
                "transport": "cli", "catalogIdleRetirement": "0",
                "connectedAccounts": ["default", "ads"], "enabledJobs": ["fixture-promo"]}

    def test_readiness_rejects_wrong_deployment_source_flags_accounts_and_jobs(self):
        RELEASE.validate_readiness(self.report(), "new", SHA, "0", {"fixture-promo"})
        for key, wrong in [("ready", False), ("deploymentId", "old"), ("sourceCommit", "b"*40),
                           ("transport", "rpc"), ("catalogIdleRetirement", "1"),
                           ("connectedAccounts", ["default"]), ("enabledJobs", [])]:
            report = self.report()
            report[key] = wrong
            with self.subTest(key=key), self.assertRaises(ValueError):
                RELEASE.validate_readiness(report, "new", SHA, "0", {"fixture-promo"})

    def test_readiness_rejects_retired_feeds_without_requiring_them(self):
        RELEASE.validate_readiness(self.report(), "new", SHA, "0", {"fixture-promo"})
        for name in ("vacancy-lamp-feed", "hustlers-feed", "job-scout-group-feed",
                     "job-scout-hustlers-feed"):
            report = self.report()
            report["enabledJobs"].append(name)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "retired job feeds"):
                RELEASE.validate_readiness(report, "new", SHA, "0", {"fixture-promo"})

    def test_startup_verification_allows_the_entrypoint_lease_handoff(self):
        # A stale gateway lease can take 330 seconds to expire before startup.
        # Both the new image and exact-image rollback must wait through that.
        for read in [lambda: RELEASE.startup_result("new", SHA, "0", {"fixture-promo"}),
                     lambda: RELEASE.verify_rollback_startup("new")]:
            clock = {"now": 0}
            def wait(seconds): clock["now"] += seconds
            def logs(*args, **kwargs):
                text = json.dumps({"message": RELEASE.PREFIX+json.dumps(self.report())}) if clock["now"] >= 335 else ""
                return subprocess.CompletedProcess(args=[], returncode=0, stdout=text)
            with self.subTest(reader=read), \
                 patch.object(RELEASE.time, "monotonic", side_effect=lambda: clock["now"]), \
                 patch.object(RELEASE.time, "sleep", side_effect=wait), \
                 patch.object(RELEASE.subprocess, "run", side_effect=logs):
                read()
            self.assertGreaterEqual(clock["now"], 335)

    def test_source_archive_excludes_untracked_and_uncommitted_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "fixture@example.test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Fixture"], check=True)
            (root / "tracked.txt").write_text("committed")
            subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
            sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
            (root / "tracked.txt").write_text("unrelated local edit")
            (root / ".env").write_text("fixture-private-data")
            archive = Path(temp) / "archive"
            archive.mkdir()
            RELEASE.archive_commit(root, sha, archive)
            self.assertEqual((archive / "tracked.txt").read_text(), "committed")
            self.assertFalse((archive / ".env").exists())

    def test_rollback_restores_owned_settings_and_exact_previous_image(self):
        calls = []

        def railway(*args, **kwargs):
            calls.append(args)
            if args[0] == "api":
                self.assertIn('"previous"', args[-1])
                return {"data": {"deploymentRedeploy": {"id": "rollback"}}}
            return {}
        with patch.object(RELEASE, "railway", side_effect=railway), patch.object(RELEASE, "verify_rollback_startup"), patch.object(RELEASE, "wait_for", return_value={
            "id": "rollback", "meta": {"imageDigest": "sha256:previous"}}):
            report = RELEASE.restore({"id": "previous", "meta": {"imageDigest": "sha256:previous"}}, {
                "SOURCE_COMMIT": None, "GENAIE_CAMPAIGN_TRANSPORT": "cli", "GENAIE_CATALOG_IDLE_RETIREMENT": "0"})
        self.assertEqual(report["imageDigest"], "sha256:previous")
        self.assertTrue(any(args[:3] == ("variable", "delete", "SOURCE_COMMIT") for args in calls))

    def test_pending_failed_candidate_is_cancelled_by_its_exact_id(self):
        with patch.object(RELEASE, "deployments", side_effect=[
            [{"id": "new", "status": "BUILDING"}], [{"id": "new", "status": "REMOVED"}]]), \
             patch.object(RELEASE, "railway", return_value={"data": {"deploymentCancel": True}}) as railway, \
             patch.object(RELEASE.time, "sleep"):
            RELEASE.cancel_pending("new")
        self.assertIn("deploymentCancel", railway.call_args.args[1])
        self.assertEqual(json.loads(railway.call_args.args[-1]), {"id": "new"})

    def test_missing_previous_digest_stops_before_any_production_mutation(self):
        with patch.object(RELEASE.subprocess, "check_output", side_effect=[SHA+"\n", SHA+"\trefs/heads/main\n"]), \
             patch.object(RELEASE, "deployments", return_value=[{"id": "old", "status": "SUCCESS"}]), \
             patch.object(RELEASE, "railway") as railway:
            with self.assertRaisesRegex(RuntimeError, "image digest"):
                RELEASE.release(ROOT, SHA, "baseline", {})
        railway.assert_not_called()

    def test_wrong_rollback_image_is_not_reported_as_recovered(self):
        with patch.object(RELEASE, "railway", return_value={"data": {"deploymentRedeploy": {"id": "rollback"}}}), \
             patch.object(RELEASE, "wait_for", return_value={"id": "rollback", "meta": {"imageDigest": "other"}}):
            with self.assertRaisesRegex(RuntimeError, "digest"):
                RELEASE.restore({"id": "previous", "meta": {"imageDigest": "expected"}}, {})

    def test_polling_follows_the_requested_deployment_not_the_latest(self):
        with patch.object(RELEASE, "deployments", return_value=[
            {"id": "unrelated", "status": "SUCCESS"}, {"id": "wanted", "status": "CRASHED"}]):
            with self.assertRaisesRegex(RuntimeError, "CRASHED"):
                RELEASE.wait_for("wanted", timeout=1)

    def test_failed_smoke_triggers_rollback_and_does_not_leak_unowned_settings(self):
        previous = {"id": "previous", "status": "SUCCESS", "meta": {"imageDigest": "old"}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def archive(source, sha, destination):
                (Path(destination) / "deploy").mkdir()
                (Path(destination) / "deploy/promo-crons.json").write_text('{"promos":[{"name":"fixture-promo"}]}')

            def railway(*args, **kwargs):
                if args[:2] == ("variable", "list"):
                    return {"SOURCE_COMMIT": "old", "GENAIE_CATALOG_IDLE_RETIREMENT": "1",
                            "OTHER_SECRET": "fixture-private"}
                if args[0] == "up": return {"deploymentId": "new"}
                return {}

            report = {}
            with patch.object(RELEASE.subprocess, "check_output", side_effect=[SHA+"\n", SHA+"\trefs/heads/main\n"]), \
                 patch.object(RELEASE, "deployments", return_value=[previous]), \
                 patch.object(RELEASE, "archive_commit", side_effect=archive), \
                 patch.object(RELEASE, "railway", side_effect=railway), \
                 patch.object(RELEASE, "wait_for", return_value={"meta": {"imageDigest": "new"}}), \
                 patch.object(RELEASE, "startup_result", side_effect=ValueError("wrong source SHA")), \
                 patch.object(RELEASE, "cancel_pending") as cancel, \
                 patch.object(RELEASE, "restore", return_value={"deploymentId": "rollback"}) as restore:
                with self.assertRaisesRegex(ValueError, "wrong source"):
                    RELEASE.release(root, SHA, "preserve", report)
            cancel.assert_called_once_with("new")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(restore.call_args.args[0]["id"], "previous")
            self.assertEqual(restore.call_args.args[1]["GENAIE_CATALOG_IDLE_RETIREMENT"], "1")
            self.assertNotIn("OTHER_SECRET", restore.call_args.args[1])
            self.assertNotIn("fixture-private", json.dumps(report))

    def test_ambiguous_upload_is_flagged_even_if_previous_image_recovers(self):
        previous = {"id": "previous", "status": "SUCCESS", "meta": {"imageDigest": "old"}}
        def archive(source, sha, destination):
            (Path(destination) / "deploy").mkdir()
            (Path(destination) / "deploy/promo-crons.json").write_text('{"promos":[]}')
        def railway(*args, **kwargs):
            if args[0] == "up": raise TimeoutError("upload timed out")
            return {}
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(RELEASE.subprocess, "check_output", side_effect=[SHA+"\n", SHA+"\trefs/heads/main\n"]), \
             patch.object(RELEASE, "deployments", return_value=[previous]), \
             patch.object(RELEASE, "archive_commit", side_effect=archive), \
             patch.object(RELEASE, "railway", side_effect=railway), \
             patch.object(RELEASE, "restore", return_value={"startupVerified": True}):
            report = {}
            with self.assertRaisesRegex(TimeoutError, "upload timed out"):
                RELEASE.release(Path(temp), SHA, "baseline", report)
        self.assertIn("no deployment ID", report["candidateCancellationError"])
        self.assertIn("release:", report["releaseMessage"])
        self.assertEqual(report["status"], "failed")

    def test_superseded_commit_never_mutates_railway(self):
        report = {}
        with patch.object(RELEASE.subprocess, "check_output", side_effect=[SHA+"\n", "b"*40+"\trefs/heads/main\n"]), \
             patch.object(RELEASE, "railway") as railway:
            RELEASE.release(ROOT, SHA, "baseline", report)
        self.assertEqual(report["status"], "superseded")
        railway.assert_not_called()


class ReadinessTests(unittest.TestCase):
    def test_report_has_provenance_and_job_names_without_private_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / "state").mkdir()
            (state / "openclaw.json").write_text(json.dumps({"channels": {"whatsapp": {
                "accounts": {"default": {}, "ads": {}}}}, "bindings": [{"private": "fixture-private"}]}))
            marker = state / "marker"
            marker.write_text(f"Source commit: {SHA}\n")
            with closing(sqlite3.connect(state / "state/openclaw.sqlite")) as database:
                database.execute("CREATE TABLE cron_jobs(name TEXT, enabled INTEGER)")
                database.executemany("INSERT INTO cron_jobs VALUES (?,?)", [("fixture-promo", 1), ("private-job", 1)])
                database.commit()
            channels = {"channelAccounts": {"whatsapp": [{"accountId": key, "connected": True}
                                                        for key in ["default", "ads"]]}}
            report = READY.inspect(state, marker, channels, {"RAILWAY_DEPLOYMENT_ID": "new"})
            self.assertEqual(report["sourceCommit"], SHA)
            self.assertEqual(report["enabledJobs"], ["fixture-promo"])
            self.assertNotIn("fixture-private", json.dumps(report))
            channels["channelAccounts"]["whatsapp"].pop()
            with self.assertRaises(ValueError):
                READY.inspect(state, marker, channels, {})


if __name__ == "__main__":
    unittest.main()
