"""Tests for the promo cron manifest, drift comparator and reinstall orchestrator.

The gateway's cron state is the one part of this system a merge to main cannot change,
so these tests guard the two things that can still rot silently: the repo disagreeing
with itself (manifest vs. the eight wrappers vs. install-promo.sh vs. the READMEs), and
the comparator mis-reading a live payload. Nothing here contacts a gateway -- the
orchestrator tests drive it through the docker shim that deploy/gateway-exec.sh:10-13
keeps for exactly this purpose.
"""

import copy
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "deploy/promo-crons.json"
ORCHESTRATOR = ROOT / "deploy/reinstall-promo-crons.sh"
COMPARATOR = ROOT / "deploy/promo-cron-drift.py"
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

# The schedules #114 chose, written out a second time on purpose: a sloppy edit that
# changes the manifest and a wrapper together still has to get past this table.
EXPECTED_SCHEDULES = {
    "borehole-drilling": "13 8 * * *",
    "borehole-paid": "15 7 * * *",
    "fruit-trees": "40 8 * * *",
    "skylight-solar": "21 13 * * *",
    "appliance-repairs": "35 10 * * *",
    "cv": "39 11 * * *",
    "building-materials": "50 12 * * *",
    "genaie": "26 14 * * *",
    "genaie-leads": "6 17 * * *",
    "big-player": "30 15 * * *",
    "industrial-lighting": "7 9 * * *",
}


def _load_comparator():
    spec = importlib.util.spec_from_file_location("promo_cron_drift", COMPARATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drift = _load_comparator()


def local_time(expr):
    """'13 8 * * *' -> '08:13'. The READMEs are checked against the cron expression
    itself, so there is no second hand-written copy of the times to go stale."""
    minute, hour = expr.split()[0], expr.split()[1]
    return "%02d:%02d" % (int(hour), int(minute))


def live_jobs(drop=(), patch=None, extra=()):
    """An in-sync payload built from the manifest, with per-job deviations applied."""
    patch = patch or {}
    jobs = []
    for promo in MANIFEST["promos"]:
        if promo["name"] in drop:
            continue
        job = {
            "id": "id-" + promo["slug"],
            "name": promo["name"],
            "enabled": True,
            "schedule": {"expr": promo["cron"], "tz": promo["tz"]},
            "payload": {
                "argv": ["python3", promo["script"]],
                "cwd": MANIFEST["commandCwd"],
                "timeoutSeconds": MANIFEST["timeoutSeconds"],
            },
            "failureAlert": {"after": 1, "to": promo["failureAlertTo"]},
        }
        for path, value in patch.get(promo["name"], {}).items():
            target = job
            parts = path.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
        jobs.append(job)
    # An unrelated gateway job must never be mistaken for a promo.
    jobs.append({"id": "jf", "name": "vacancy-lamp-feed", "enabled": True,
                 "schedule": {"expr": "20 9 * * *", "tz": "Africa/Harare"},
                 "payload": {"argv": ["/w/bin/job-feed", "deliver-group-feed"],
                             "timeoutSeconds": 600}})
    jobs.extend(copy.deepcopy(list(extra)))
    return {"jobs": jobs}


def run_comparator(payload, *args):
    """Call the comparator in-process; returns (exit code, stdout, stderr)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(payload, handle) if not isinstance(payload, str) else handle.write(payload)
        path = handle.name
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = drift.main(["promo-cron-drift.py", "--manifest", str(MANIFEST_PATH),
                               "--live", path, *args])
    finally:
        os.unlink(path)
    return code, out.getvalue(), err.getvalue()


class ManifestContractTests(unittest.TestCase):
    def test_manifest_declares_exactly_the_promo_installers(self):
        slugs = {p["slug"] for p in MANIFEST["promos"]}
        installers = {p.name[len("install-"):-len("-promo.sh")]
                      for p in ROOT.glob("deploy/install-*-promo.sh")}
        membership = json.loads((ROOT / "workspace/cron/membership.json")
                                .read_text(encoding="utf-8"))
        declared = set()
        for group in membership.get("groups", {}).values():
            declared.update(group.get("promos", []))
        self.assertEqual(slugs, installers, "manifest and installer files disagree")
        self.assertEqual(slugs, declared, "manifest and membership.json disagree")
        self.assertEqual(len(slugs), 11)

    def test_each_wrapper_passes_the_manifest_values_to_register_promo_cron(self):
        for promo in MANIFEST["promos"]:
            with self.subTest(promo=promo["slug"]):
                text = (ROOT / promo["installer"]).read_text(encoding="utf-8")
                expected = (
                    'register_promo_cron \\\n'
                    '  "%s" \\\n'
                    '  "${%s_CRON_EXPR:-%s}" \\\n'
                    '  "${%s_TZ:-%s}" \\\n'
                    '  "${%s_FAILURE_ALERT_TO:-%s}" \\\n'
                    '  "%s"'
                ) % (promo["name"], promo["envPrefix"], promo["cron"],
                     promo["envPrefix"], promo["tz"],
                     promo["envPrefix"], promo["failureAlertTo"], promo["script"])
                self.assertIn(expected, text)

    def test_manifest_pins_the_post_114_off_the_hour_schedules(self):
        self.assertEqual({p["slug"]: p["cron"] for p in MANIFEST["promos"]},
                         EXPECTED_SCHEDULES)
        for promo in MANIFEST["promos"]:
            self.assertEqual(promo["tz"], "Africa/Harare")
            self.assertNotEqual(promo["cron"].split()[0], "0",
                                "%s is back on the hour" % promo["slug"])

    def test_manifest_timeout_matches_the_install_promo_default(self):
        text = (ROOT / "deploy/install-promo.sh").read_text(encoding="utf-8")
        self.assertEqual(MANIFEST["timeoutSeconds"], 7200)
        self.assertIn('PROMO_CRON_TIMEOUT_SECONDS="${PROMO_CRON_TIMEOUT_SECONDS:-%d}"'
                      % MANIFEST["timeoutSeconds"], text)
        self.assertNotIn("--timeout-seconds 600", text)

    def test_manifest_script_paths_exist_in_the_workspace(self):
        for promo in MANIFEST["promos"]:
            with self.subTest(promo=promo["slug"]):
                rel = promo["script"].split("/.openclaw/", 1)[1]
                self.assertTrue((ROOT / rel).is_file(), "%s missing" % rel)

    def test_installer_self_verify_covers_schedule_timezone_timeout_and_enabled(self):
        text = (ROOT / "deploy/install-promo.sh").read_text(encoding="utf-8")
        for needle in (".schedule.expr == $expr", ".schedule.tz == $tz",
                       ".payload.timeoutSeconds == $timeout", ".enabled == true"):
            self.assertIn(needle, text)

    def test_self_verify_runs_after_the_failure_alert_edit(self):
        # That second edit passes no --cron/--tz. Verifying afterwards is what would
        # catch it clobbering the schedule set moments earlier; keep the order.
        text = (ROOT / "deploy/install-promo.sh").read_text(encoding="utf-8")
        self.assertLess(text.index("--failure-alert-cooldown"),
                        text.index(".schedule.expr == $expr"))

    def test_both_readmes_quote_the_manifest_schedules(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        railway = (ROOT / "deploy/railway/README.md").read_text(encoding="utf-8")
        found = dict(re.findall(r"\(`install-([a-z-]+)-promo\.sh`, (\d{2}:\d{2})\)", readme))
        self.assertEqual(found, {p["slug"]: local_time(p["cron"]) for p in MANIFEST["promos"]})
        for promo in MANIFEST["promos"]:
            with self.subTest(promo=promo["slug"]):
                shown = local_time(promo["cron"])
                self.assertIn(shown, readme)
                self.assertIn(shown, railway)
    def test_neither_readme_still_quotes_a_promos_pre_114_on_the_hour_slot(self):
        """The deploy/railway/README.md:159 "11:00" bug, made unreintroducible.

        Scoped by proximity rather than by a bare substring: other times appear in
        the READMEs too, so only an on-the-hour time whose surrounding prose names
        a promo that moved off the hour is stale.
        """
        hours = {int(local_time(p["cron"]).split(":")[0]): p for p in MANIFEST["promos"]}
        for label, text in (("README.md", ROOT / "README.md"),
                            ("deploy/railway/README.md", ROOT / "deploy/railway/README.md")):
            body = text.read_text(encoding="utf-8")
            for match in re.finditer(r"(\d{1,2}):00(?= ?`?Africa/Harare)", body):
                promo = hours.get(int(match.group(1)))
                if promo is None:
                    continue
                window = body[max(0, match.start() - 200):match.start()].lower()
                names = (promo["name"].lower(), promo["slug"].lower(),
                         Path(promo["installer"]).name.lower())
                if any(name in window for name in names):
                    self.fail("%s quotes %s:00 for %s, which moved to %s in #114"
                              % (label, match.group(1), promo["slug"],
                                 local_time(promo["cron"])))

    def test_the_orchestrator_defers_every_write_to_the_installers(self):
        text = ORCHESTRATOR.read_text(encoding="utf-8")
        for forbidden in ("cron add", "cron edit", "cron rm", "--timeout-seconds",
                          "--failure-alert"):
            self.assertNotIn(forbidden, text,
                             "orchestrator reimplements register_promo_cron: %s" % forbidden)
        self.assertIn("gateway-exec.sh", text)
        self.assertEqual(text.count("cron list --all --json"), 1)

    def test_the_new_deploy_scripts_are_executable(self):
        for path in (ORCHESTRATOR, COMPARATOR):
            self.assertTrue(os.access(path, os.X_OK), "%s is not executable" % path.name)


class DriftComparatorTests(unittest.TestCase):
    def test_in_sync_gateway_state_reports_no_drift(self):
        code, out, _ = run_comparator(live_jobs())
        self.assertEqual(code, 0)
        self.assertIn("11 ok, 0 drifted, 0 missing", out)

    def test_the_recorded_2026_10_16_state_is_reported_as_drift(self):
        # All eight on the hour with the old 600 s timeout: what production ran the
        # day after #114 merged.
        patch = {p["name"]: {"schedule.expr": "0 %s * * *" % p["cron"].split()[1],
                             "payload.timeoutSeconds": 600}
                 for p in MANIFEST["promos"]}
        code, out, _ = run_comparator(live_jobs(patch=patch))
        self.assertEqual(code, 1)
        self.assertIn("0 ok, 11 drifted", out)
        self.assertIn("schedule.expr", out)
        self.assertIn("payload.timeoutSeconds", out)
        self.assertIn("reinstall-promo-crons.sh", out)

    def test_a_stale_timeout_alone_is_drift(self):
        code, out, _ = run_comparator(live_jobs(patch={"cv-promo": {"payload.timeoutSeconds": 600}}))
        self.assertEqual(code, 1)
        self.assertIn("10 ok, 1 drifted", out)

    def test_a_stale_schedule_alone_is_drift(self):
        code, _, _ = run_comparator(live_jobs(patch={"cv-promo": {"schedule.expr": "0 11 * * *"}}))
        self.assertEqual(code, 1)

    def test_a_wrong_timezone_is_drift(self):
        code, out, _ = run_comparator(live_jobs(patch={"cv-promo": {"schedule.tz": "UTC"}}))
        self.assertEqual(code, 1)
        self.assertIn("schedule.tz", out)

    def test_a_disabled_job_is_drift_even_with_the_right_schedule(self):
        code, out, _ = run_comparator(live_jobs(patch={"cv-promo": {"enabled": False}}))
        self.assertEqual(code, 1)
        self.assertIn("enabled", out)

    def test_a_wrong_command_argv_is_drift(self):
        code, out, _ = run_comparator(
            live_jobs(patch={"cv-promo": {"payload.argv": ["python3", "/w/cron/cv-promo/other.py"]}}))
        self.assertEqual(code, 1)
        self.assertIn("payload.argv", out)

    def test_post_ad_does_not_satisfy_the_cv_promos_post_cv_ad(self):
        code, _, _ = run_comparator(live_jobs(patch={"cv-promo": {
            "payload.argv": ["python3", "/home/joseph/.openclaw/workspace/cron/cv-promo/post_ad.py"]}}))
        self.assertEqual(code, 1)

    def test_a_missing_job_is_reported_as_missing_not_as_matching(self):
        code, out, _ = run_comparator(live_jobs(drop=("genaie-promo",)))
        self.assertEqual(code, 1)
        self.assertIn("MISSING", out)
        self.assertIn("1 missing", out)

    def test_an_undeclared_promo_shaped_job_is_reported_with_a_removal_command(self):
        # The duplicate-send trap: register_promo_cron matches on .name only, so this
        # job keeps firing the same ad on the old schedule forever.
        ghost = {"id": "ghost1", "name": "cv-promo-old", "enabled": True,
                 "schedule": {"expr": "0 11 * * *", "tz": "Africa/Harare"},
                 "payload": {"argv": ["python3", "/home/joseph/.openclaw/workspace/"
                                                 "cron/cv-promo/post_cv_ad.py"],
                             "timeoutSeconds": 600}}
        code, out, _ = run_comparator(live_jobs(extra=[ghost]))
        self.assertEqual(code, 1)
        self.assertIn("UNDECLARED cv-promo-old", out)
        self.assertIn("openclaw cron rm ghost1", out)
        self.assertIn("1 undeclared", out)

    def test_an_unrelated_job_is_ignored(self):
        code, out, _ = run_comparator(live_jobs())
        self.assertEqual(code, 0)
        self.assertNotIn("vacancy-lamp-feed", out)

    def test_failure_alert_and_cwd_are_reported_but_do_not_fail_the_check(self):
        code, out, _ = run_comparator(live_jobs(patch={"cv-promo": {"payload.cwd": "/elsewhere"}}))
        self.assertEqual(code, 0, "cwd must not be gated while its JSON key is unverified")
        self.assertIn("informational", out)

    def test_reported_phone_numbers_are_redacted(self):
        report = drift.classify(MANIFEST, live_jobs()["jobs"])
        rendered = json.dumps(report["informational"])
        for promo in MANIFEST["promos"]:
            self.assertNotIn(promo["failureAlertTo"], rendered)
        self.assertIn("...", rendered)

    def test_unparseable_input_exits_two_instead_of_reporting_sync(self):
        code, _, err = run_comparator("not json at all")
        self.assertEqual(code, 2)
        self.assertIn("unusable", err)

    def test_input_without_a_jobs_list_exits_two(self):
        code, _, _ = run_comparator({"crons": []})
        self.assertEqual(code, 2)

    def test_only_narrows_the_comparison_to_the_named_promos(self):
        patch = {"genaie-promo": {"payload.timeoutSeconds": 600}}
        self.assertEqual(run_comparator(live_jobs(patch=patch), "--only", "cv")[0], 0)
        self.assertEqual(run_comparator(live_jobs(patch=patch), "--only", "genaie")[0], 1)

    def test_an_unknown_only_target_is_a_usage_error(self):
        code, _, err = run_comparator(live_jobs(), "--only", "nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown promo", err)

    def test_a_closed_pipe_does_not_print_a_traceback(self):
        """`--check | head` is how anyone reads an eight-promo drift report."""
        patch = {p["name"]: {"payload.timeoutSeconds": 600} for p in MANIFEST["promos"]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(live_jobs(patch=patch), handle)
            path = handle.name
        try:
            proc = subprocess.run(
                "%s %s --manifest %s --live %s | head -3"
                % (sys.executable, COMPARATOR, MANIFEST_PATH, path),
                shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False)
        finally:
            os.unlink(path)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("BrokenPipeError", proc.stderr)

    def test_json_output_lists_every_class_and_the_drifted_field_names(self):
        code, out, _ = run_comparator(
            live_jobs(patch={"cv-promo": {"payload.timeoutSeconds": 600}}), "--json")
        self.assertEqual(code, 1)
        report = json.loads(out)
        for key in ("ok", "drifted", "missing", "undeclared", "legacy", "informational"):
            self.assertIn(key, report)
        self.assertEqual(report["drifted"][0]["name"], "cv-promo")
        self.assertEqual([f["field"] for f in report["drifted"][0]["fields"]],
                         ["payload.timeoutSeconds"])


SHIM = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_DOCKER_CALLS"
if printf '%s\\n' "$*" | grep -q 'cron list'; then
  if [ -n "${FAKE_CRON_FAIL:-}" ]; then printf 'ssh: connect failed\\n' >&2; exit 1; fi
  cat "$FAKE_CRON_JOBS"; exit 0
fi
exit 0
"""


class OrchestratorTests(unittest.TestCase):
    """Drive the real orchestrator and the real installers against a fake gateway,
    using the docker mode deploy/gateway-exec.sh keeps for installer tests."""

    def run_orchestrator(self, *args, payload=None, fail=False, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shim, calls, jobs = tmp / "shim", tmp / "calls.log", tmp / "jobs.json"
            shim.write_text(SHIM, encoding="utf-8")
            shim.chmod(0o755)
            calls.write_text("", encoding="utf-8")
            jobs.write_text(json.dumps(payload if payload is not None else live_jobs()),
                            encoding="utf-8")
            full = {**os.environ, "OPENCLAW_EXEC_MODE": "docker", "DOCKER": str(shim),
                    "FAKE_DOCKER_CALLS": str(calls), "FAKE_CRON_JOBS": str(jobs)}
            if fail:
                full["FAKE_CRON_FAIL"] = "1"
            full.update(env or {})
            proc = subprocess.run(["bash", str(ORCHESTRATOR), *args], env=full, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            return proc, calls.read_text(encoding="utf-8")

    def test_dry_run_plans_every_promo_without_touching_the_gateway(self):
        proc, calls = self.run_orchestrator("--dry-run", env={"DOCKER": "/nonexistent"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("11 promo(s) planned", proc.stdout)
        self.assertEqual(calls.strip(), "")

    def test_apply_runs_all_installers_in_schedule_order(self):
        proc, calls = self.run_orchestrator()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("11 of 11 installer(s) succeeded", proc.stdout)
        self.assertIn("11 ok, 0 drifted", proc.stdout)
        seen = [p["name"] for p in MANIFEST["promos"]
                if re.search(r"--name %s\b" % re.escape(p["name"]), calls)]
        self.assertEqual(seen, [p["name"] for p in MANIFEST["promos"]])

    def test_one_failing_installer_does_not_stop_the_others(self):
        # cv-promo's live row never matches, so register_promo_cron's self-verify fails.
        payload = live_jobs(patch={"cv-promo": {"schedule.expr": "0 11 * * *"}})
        proc, calls = self.run_orchestrator(payload=payload)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("10 of 11 installer(s) succeeded", proc.stdout)
        self.assertIn("cv-promo", proc.stderr)
        for promo in MANIFEST["promos"]:
            self.assertRegex(calls, r"--name %s\b" % re.escape(promo["name"]))

    def test_an_unreachable_gateway_aborts_before_running_any_installer(self):
        proc, calls = self.run_orchestrator(fail=True)
        self.assertEqual(proc.returncode, 75)
        self.assertIn("nothing was changed", proc.stderr.lower())
        self.assertEqual(calls.count("cron list"), 1)
        self.assertNotIn("--name", calls)

    def test_check_mode_makes_one_read_and_no_writes(self):
        proc, calls = self.run_orchestrator("--check")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls.count("cron list"), 1)
        for write in ("cron add", "cron edit", "cron rm"):
            self.assertNotIn(write, calls)

    def test_check_mode_reports_the_recorded_2026_10_16_drift(self):
        patch = {p["name"]: {"schedule.expr": "0 %s * * *" % p["cron"].split()[1],
                             "payload.timeoutSeconds": 600}
                 for p in MANIFEST["promos"]}
        proc, _ = self.run_orchestrator("--check", payload=live_jobs(patch=patch))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("0 ok, 11 drifted", proc.stdout)

    def test_only_limits_the_run_to_the_named_promos(self):
        proc, calls = self.run_orchestrator("--only", "cv", "--only", "genaie-leads-promo")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 of 2 installer(s) succeeded", proc.stdout)
        self.assertNotIn("--name genaie-promo ", calls)

    def test_unknown_only_target_and_unknown_flag_are_usage_errors(self):
        self.assertEqual(self.run_orchestrator("--only", "nope")[0].returncode, 2)
        self.assertEqual(self.run_orchestrator("--oops")[0].returncode, 2)

    def test_help_exits_zero_and_names_every_flag(self):
        proc, _ = self.run_orchestrator("--help")
        self.assertEqual(proc.returncode, 0)
        for flag in ("--check", "--only", "--dry-run", "--json", "--allow-overrides"):
            self.assertIn(flag, proc.stdout)

    def test_a_disagreeing_env_override_is_refused_unless_allowed(self):
        refused, _ = self.run_orchestrator("--dry-run",
                                           env={"PROMO_CRON_TIMEOUT_SECONDS": "600"})
        self.assertEqual(refused.returncode, 2)
        self.assertIn("PROMO_CRON_TIMEOUT_SECONDS", refused.stderr)
        allowed, _ = self.run_orchestrator("--dry-run", "--allow-overrides",
                                           env={"PROMO_CRON_TIMEOUT_SECONDS": "600"})
        self.assertEqual(allowed.returncode, 0)

    def test_check_mode_never_writes_a_repo_file(self):
        watched = [MANIFEST_PATH, ORCHESTRATOR, COMPARATOR] + \
                  [ROOT / p["installer"] for p in MANIFEST["promos"]]
        before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in watched}
        self.run_orchestrator("--check")
        after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in watched}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
