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
                    return {"SOURCE_COMMIT": "old", "OTHER_SECRET": "fixture-private"}
                if args[0] == "up": return {"deploymentId": "new"}
                return {}

            report = {}
            with patch.object(RELEASE.subprocess, "check_output", side_effect=[SHA+"\n", SHA+"\trefs/heads/main\n"]), \
                 patch.object(RELEASE, "deployments", return_value=[previous]), \
                 patch.object(RELEASE, "archive_commit", side_effect=archive), \
                 patch.object(RELEASE, "railway", side_effect=railway), \
                 patch.object(RELEASE, "wait_for", return_value={"meta": {"imageDigest": "new"}}), \
                 patch.object(RELEASE, "startup_result", side_effect=ValueError("wrong source SHA")), \
                 patch.object(RELEASE, "restore", return_value={"deploymentId": "rollback"}) as restore:
                with self.assertRaisesRegex(ValueError, "wrong source"):
                    RELEASE.release(root, SHA, "baseline", report)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(restore.call_args.args[0]["id"], "previous")
            self.assertNotIn("OTHER_SECRET", restore.call_args.args[1])
            self.assertNotIn("fixture-private", json.dumps(report))

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
            with sqlite3.connect(state / "state/openclaw.sqlite") as database:
                database.execute("CREATE TABLE cron_jobs(name TEXT, enabled INTEGER)")
                database.executemany("INSERT INTO cron_jobs VALUES (?,?)", [("fixture-promo", 1), ("private-job", 1)])
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
