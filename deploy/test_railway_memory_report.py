import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("memory_report", Path(__file__).with_name("railway-memory-report.py"))
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class MemoryReportTests(unittest.TestCase):
    def event(self):
        return {"type": "diagnostic.memory.sample", "ts": 100_000,
                "sessionKey": "private", "memory": {
                    "rssBytes": 1000, "workerCount": 3, "secret": "private",
                    "workerHeaps": [
                        {"script": "session-transcript.worker.js", "heapUsed": 20, "heapTotal": 30,
                         "external": 10, "arrayBuffers": 5, "sampleAgeMs": 30_000},
                        {"script": "session-transcript.worker.js", "heapUsed": 40, "heapTotal": 50,
                         "external": 10, "sampleAgeMs": 30_001},
                        {"script": "/private/customer.js", "heapUsed": 1, "heapTotal": 2,
                         "external": 0, "sampleAgeMs": 0}]}}

    def test_groups_workers_without_double_counting_buffers_or_exporting_private_data(self):
        report = REPORT.worker_sample(self.event(), 120_000)
        group = report["workerGroups"][0]
        self.assertEqual(group["heapUsedBytes"], 60)
        self.assertEqual(group["externalBytes"], 20)
        self.assertEqual(group["arrayBuffersBytes"], 5)
        self.assertEqual(group["arrayBuffersSampledWorkers"], 1)
        self.assertTrue(report["fresh"])
        self.assertNotIn("private", json.dumps(report))

    def test_freshness_includes_worker_sample_age_and_rejects_future_time(self):
        self.assertFalse(REPORT.worker_sample(self.event(), 140_000)["fresh"])
        self.assertFalse(REPORT.worker_sample(self.event(), 99_999)["fresh"])
        self.assertFalse(REPORT.worker_sample(self.event(), 200_000)["fresh"])

    def test_malformed_counters_cannot_pollute_totals(self):
        event = self.event()
        event["memory"]["workerHeaps"].append({"script": "bad", "heapUsed": float("nan")})
        event["memory"]["rssBytes"] = True
        report = REPORT.worker_sample(event, 120_000)
        self.assertNotIn("rssBytes", report["memory"])
        self.assertNotIn("bad", [group["script"] for group in report["workerGroups"]])

    def test_process_identity_and_pss_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary) / "42"
            proc.mkdir()
            (proc / "comm").write_text("openclaw-gatewa\n")
            (proc / "status").write_text("VmRSS: 100 kB\nRssAnon: 80 kB\nRssFile: 20 kB\nThreads: 9\n")
            (proc / "smaps_rollup").write_text("Pss: 90 kB\nPrivate_Dirty: 70 kB\n")
            (proc / "stat").write_text("42 (process with spaces) " + " ".join(["0"] * 19 + ["123"]))
            report = REPORT.process_snapshot(proc)
            self.assertEqual(report["pssBytes"], 90 * 1024)
            self.assertEqual(report["startTicks"], 123)
            (proc / "smaps_rollup").unlink()
            self.assertTrue(REPORT.process_snapshot(proc)["pssUnavailable"])

    def test_capture_scopes_ssh_and_never_returns_raw_command_output(self):
        response = type("Result", (), {"returncode": 0, "stdout": REPORT.PREFIX + '{"gatewayRunning": false}\n'})()
        with patch.object(REPORT.subprocess, "run", return_value=response) as run:
            self.assertFalse(REPORT.capture()["gatewayRunning"])
            command = run.call_args.args[0]
            self.assertIn(REPORT.SERVICE, command)
            self.assertIn("--remote", command)
        response.returncode = 1
        response.stderr = "secret"
        with patch.object(REPORT.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "no config changes or restarts"):
                REPORT.capture()

    def test_cache_trial_only_advises_installed_regular_code_without_changing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "code.mjs").write_text("export const value = 42;\n")
            (root / "state.sqlite").write_bytes(b"preserved database")
            (root / "credentials").write_bytes(b"preserved credentials")
            outside = root / "outside"
            outside.mkdir()
            (outside / "other.mjs").write_text("outside")
            source = root / "dist"
            source.mkdir()
            (source / "module.mjs").write_text("module")
            (source / "escape.mjs").symlink_to(root / "credentials")
            (source / "linked").symlink_to(outside, target_is_directory=True)
            (source / "data.sqlite").write_bytes(b"untouched")
            os.mkfifo(source / "fifo.js")
            advised = []
            with patch.object(REPORT.os, "posix_fadvise", side_effect=lambda fd, *_: advised.append(os.readlink(f"/proc/self/fd/{fd}"))):
                result = REPORT.release_source_cache(source)
            self.assertEqual(advised, [str(source / "module.mjs")])
            self.assertEqual(result["filesAdvised"], 1)
            self.assertEqual((source / "module.mjs").read_text(), "module")
            self.assertEqual((source / "data.sqlite").read_bytes(), b"untouched")

    def test_symlinked_root_and_platform_without_advice_fail_before_any_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "dist").mkdir()
            (root / "linked").symlink_to(root / "dist", target_is_directory=True)
            with patch.object(REPORT.os, "posix_fadvise") as advice:
                with self.assertRaises(ValueError):
                    REPORT.release_source_cache(root / "linked")
                advice.assert_not_called()


if __name__ == "__main__":
    unittest.main()
