import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/railway/sync-reply-gates.py"
TEMPLATE = ROOT / "config/openclaw.template.json"
APPLIER = ROOT / "deploy/patch-openclaw-config.py"

SPEC = importlib.util.spec_from_file_location("sync_reply_gates", SCRIPT)
SYNC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SYNC)

APPLIER_SPEC = importlib.util.spec_from_file_location("patch_openclaw_config", APPLIER)
PATCHER = importlib.util.module_from_spec(APPLIER_SPEC)
assert APPLIER_SPEC.loader is not None
APPLIER_SPEC.loader.exec_module(PATCHER)


def tracked_template() -> dict:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


class ReplyGateSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.template = self.dir / "template.json"
        self.config = self.dir / "openclaw.json"
        self.template.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")

        # A live config shaped like production: gates off, plus the live-only
        # plugin keys, peer overrides and generated bindings the template lacks.
        self.live = {
            "channels": {
                "whatsapp": {
                    "enabled": True,
                    "dmPolicy": "allowlist",
                    "groupPolicy": "disabled",
                    "allowFrom": ["+263785632943"],
                    "accounts": {"default": {"enabled": False}, "ads": {"enabled": True}},
                    "direct": {
                        "*": {"systemPrompt": "live prompt", "historyLimit": 20},
                        "+263111111111": {"systemPrompt": "peer override"},
                    },
                }
            },
            "plugins": {
                "entries": {
                    "whatsapp": {"enabled": True},
                    "public-service-gate": {
                        "enabled": True,
                        "config": {
                            "enabled": False,
                            "botWhatsappNumber": "+263785632943",
                            "whatsappAdminUsers": ["+263785632943"],
                            "retiredButDeclared": "keep me",
                            "cvMakerPaymentDetails": "EcoCash +263700000000 (Old Payee)",
                        },
                    },
                    "whatsapp-relevance-gate": {
                        "config": {"enabled": True, "relevanceMode": "embedding"}
                    },
                }
            },
            "bindings": [
                {
                    "agentId": "main",
                    "match": {
                        "channel": "whatsapp",
                        "accountId": "*",
                        "peer": {"kind": "direct", "id": "+263785632943"},
                    },
                },
                {
                    "agentId": "public",
                    "match": {
                        "channel": "whatsapp",
                        "accountId": "*",
                        "peer": {"kind": "direct", "id": "*"},
                    },
                },
            ],
        }
        self.write_live()

    def write_live(self) -> None:
        self.config.write_text(json.dumps(self.live, indent=2), encoding="utf-8")
        self.config.chmod(0o600)

    def sync(self, **kwargs):
        return SYNC.sync(self.template, self.config, APPLIER, **kwargs)

    def read(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def run_script(self, *args, env=None):
        environ = dict(os.environ)
        environ.pop("REPLIES_DISABLED", None)
        environ.update(env or {})
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, check=False, env=environ,
        )

    # --- forcing -----------------------------------------------------------

    def test_forces_each_gated_path_to_the_template_value(self) -> None:
        status, lines = self.sync()
        self.assertEqual(status, "updated")
        synced, template = self.read(), tracked_template()
        for path in SYNC.GATED_PATHS:
            _, want = PATCHER.get_path(template, path)
            found, have = PATCHER.get_path(synced, path)
            self.assertTrue(found, f"{SYNC.dotted(path)} missing after sync")
            self.assertEqual(have, want, SYNC.dotted(path))
        joined = " ".join(lines)
        self.assertIn("channels.whatsapp.dmPolicy", joined)
        self.assertIn("channels.whatsapp.accounts.default.enabled", joined)

    def test_leaves_every_other_key_untouched(self) -> None:
        self.sync()
        synced = self.read()
        expected = json.loads(json.dumps(self.live))
        w = expected["channels"]["whatsapp"]
        w["dmPolicy"] = "open"
        w["allowFrom"] = ["+263785632943", "*"]
        w["accounts"]["default"]["enabled"] = True
        entries = expected["plugins"]["entries"]
        entries["public-service-gate"]["config"]["enabled"] = True
        entries["public-service-gate"]["config"]["cvMakerPaymentDetails"] = (
            "EcoCash +263775489417 (Noriah Mzenda)"
        )
        entries["whatsapp-relevance-gate"]["config"]["enabled"] = False
        entries["whatsapp-relevance-gate"]["config"]["relevanceMode"] = "off"
        self.assertEqual(synced, expected)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_preserves_live_only_keys_and_peer_overrides(self) -> None:
        self.sync()
        cfg = self.read()["plugins"]["entries"]["public-service-gate"]["config"]
        self.assertEqual(cfg["retiredButDeclared"], "keep me")
        self.assertEqual(cfg["whatsappAdminUsers"], ["+263785632943"])
        self.assertEqual(cfg["botWhatsappNumber"], "+263785632943")
        direct = self.read()["channels"]["whatsapp"]["direct"]
        self.assertEqual(direct["*"]["systemPrompt"], "live prompt")
        self.assertEqual(direct["+263111111111"]["systemPrompt"], "peer override")

    def test_never_enables_the_ads_line_or_opens_groups(self) -> None:
        self.sync()
        w = self.read()["channels"]["whatsapp"]
        self.assertTrue(w["accounts"]["ads"]["enabled"])
        self.assertEqual(w["groupPolicy"], "disabled")

        self.live["channels"]["whatsapp"]["accounts"]["ads"]["enabled"] = False
        self.write_live()
        self.sync()
        self.assertFalse(self.read()["channels"]["whatsapp"]["accounts"]["ads"]["enabled"])

    def test_second_run_is_a_no_op(self) -> None:
        self.sync()
        before = self.config.stat().st_mtime_ns
        status, lines = self.sync()
        self.assertEqual(status, "unchanged")
        self.assertEqual(self.config.stat().st_mtime_ns, before)
        self.assertIn("unchanged", " ".join(lines))

    def test_creates_a_missing_gated_path(self) -> None:
        self.config.write_text(json.dumps({"channels": {"whatsapp": {}}}), encoding="utf-8")
        status, lines = self.sync()
        self.assertEqual(status, "updated")
        synced = self.read()
        self.assertEqual(synced["channels"]["whatsapp"]["dmPolicy"], "open")
        self.assertTrue(synced["channels"]["whatsapp"]["accounts"]["default"]["enabled"])
        self.assertIn("<absent>", " ".join(lines))

    # --- the allowFrom coupling -------------------------------------------

    def test_appends_the_wildcard_without_disturbing_other_entries(self) -> None:
        self.live["channels"]["whatsapp"]["allowFrom"] = ["+263785632943", "+263790453292"]
        self.write_live()
        self.sync()
        self.assertEqual(
            self.read()["channels"]["whatsapp"]["allowFrom"],
            ["+263785632943", "+263790453292", "*"],
        )

    def test_does_not_duplicate_an_existing_wildcard(self) -> None:
        self.live["channels"]["whatsapp"]["allowFrom"] = ["*", "+263785632943"]
        self.write_live()
        self.sync()
        self.assertEqual(self.read()["channels"]["whatsapp"]["allowFrom"], ["*", "+263785632943"])

    def test_creates_allowfrom_when_absent(self) -> None:
        del self.live["channels"]["whatsapp"]["allowFrom"]
        self.write_live()
        self.sync()
        self.assertEqual(self.read()["channels"]["whatsapp"]["allowFrom"], ["*"])

    def test_refuses_a_non_array_allowfrom(self) -> None:
        self.live["channels"]["whatsapp"]["allowFrom"] = "+263785632943"
        self.write_live()
        original = self.config.read_text(encoding="utf-8")
        run = self.run_script(str(self.template), str(self.config), "--applier", str(APPLIER))
        self.assertEqual(run.returncode, 0)
        self.assertIn("NOT synced", run.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)

    # --- invariants --------------------------------------------------------

    def test_preserve_paths_are_never_governed(self) -> None:
        for path in SYNC.GATED_PATHS:
            for preserve in PATCHER.PRESERVE_PATHS:
                self.assertFalse(
                    SYNC.covers(preserve, path) or SYNC.covers(path, preserve),
                    f"{SYNC.dotted(path)} overlaps PRESERVE_PATHS {SYNC.dotted(preserve)}",
                )

    def test_only_scalar_leaves_are_governed(self) -> None:
        template = tracked_template()
        for path in SYNC.GATED_PATHS:
            found, value = PATCHER.get_path(template, path)
            self.assertTrue(found, SYNC.dotted(path))
            self.assertIsInstance(value, (bool, str, int), SYNC.dotted(path))
            self.assertNotIsInstance(value, (dict, list))

    def test_never_write_covers_the_dangerous_paths(self) -> None:
        for blocked in (
            "bindings",
            "channels.whatsapp.groupPolicy",
            "channels.whatsapp.accounts.ads.enabled",
        ):
            self.assertIn(blocked, SYNC.NEVER_WRITE)
        governed = {SYNC.dotted(p) for p in SYNC.GATED_PATHS}
        self.assertEqual(governed & set(SYNC.NEVER_WRITE), set())

    def test_tracked_template_has_every_gate_in_the_expected_state(self) -> None:
        t = tracked_template()
        w = t["channels"]["whatsapp"]
        entries = t["plugins"]["entries"]
        self.assertIs(w["enabled"], True)
        self.assertEqual(w["dmPolicy"], "open")
        self.assertIs(w["accounts"]["default"]["enabled"], True)
        self.assertIs(entries["whatsapp"]["enabled"], True)
        self.assertIs(entries["public-service-gate"]["enabled"], True)
        self.assertIs(entries["public-service-gate"]["config"]["enabled"], True)
        self.assertIs(entries["whatsapp-relevance-gate"]["config"]["enabled"], False)
        self.assertEqual(entries["whatsapp-relevance-gate"]["config"]["relevanceMode"], "off")

    # --- kill switch -------------------------------------------------------

    def test_replies_disabled_turns_the_default_line_off(self) -> None:
        for value in ("1", "true", "ON", ' "true" '):
            self.write_live()
            run = self.run_script(
                str(self.template), str(self.config), "--applier", str(APPLIER),
                env={"REPLIES_DISABLED": value},
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("REPLIES_DISABLED", run.stdout)
            synced = self.read()
            self.assertFalse(synced["channels"]["whatsapp"]["accounts"]["default"]["enabled"])
            # Nothing else moved: the gates it would otherwise force stay off.
            self.assertEqual(synced["channels"]["whatsapp"]["dmPolicy"], "allowlist")
            self.assertEqual(synced["channels"]["whatsapp"]["allowFrom"], ["+263785632943"])

    def test_falsy_kill_switch_runs_the_normal_forcing_path(self) -> None:
        for value in ("0", "", "maybe"):
            self.write_live()
            run = self.run_script(
                str(self.template), str(self.config), "--applier", str(APPLIER),
                env={"REPLIES_DISABLED": value},
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(self.read()["channels"]["whatsapp"]["dmPolicy"], "open")

    # --- never fatal -------------------------------------------------------

    def test_bad_input_never_fails_the_boot_and_leaves_config_alone(self) -> None:
        original = self.config.read_text(encoding="utf-8")
        bad_template = self.dir / "bad.json"
        bad_template.write_text("not json", encoding="utf-8")
        array_template = self.dir / "array.json"
        array_template.write_text("[1, 2]", encoding="utf-8")

        for template_arg in (self.dir / "missing.json", bad_template, array_template):
            run = self.run_script(str(template_arg), str(self.config), "--applier", str(APPLIER))
            self.assertEqual(run.returncode, 0)
            self.assertIn("NOT synced", run.stderr)
            self.assertEqual(self.config.read_text(encoding="utf-8"), original)

        for config_arg in (self.dir / "missing-live.json", bad_template, array_template):
            run = self.run_script(str(self.template), str(config_arg), "--applier", str(APPLIER))
            self.assertEqual(run.returncode, 0)
            self.assertIn("NOT synced", run.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)

    # --- bindings ----------------------------------------------------------

    def test_binding_check_warns_without_rewriting_bindings(self) -> None:
        self.live["bindings"] = [{"agentId": "main", "match": {"channel": "whatsapp"}}]
        self.write_live()
        _, lines = self.sync()
        self.assertIn("WARNING", " ".join(lines))
        self.assertEqual(self.read()["bindings"], self.live["bindings"])

    def test_no_warning_when_the_public_direct_binding_is_present(self) -> None:
        _, lines = self.sync()
        self.assertNotIn("WARNING", " ".join(lines))
        self.assertEqual(len(self.read()["bindings"]), 2)

    # --- dry run -----------------------------------------------------------

    def test_dry_run_reports_without_writing(self) -> None:
        before = self.config.read_text(encoding="utf-8")
        mtime = self.config.stat().st_mtime_ns
        status, lines = self.sync(dry_run=True)
        self.assertEqual(status, "unchanged")
        self.assertIn("DRY RUN", " ".join(lines))
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)
        self.assertEqual(self.config.stat().st_mtime_ns, mtime)

    # --- wiring ------------------------------------------------------------

    def test_entrypoint_and_dockerfile_wire_the_script(self) -> None:
        entrypoint = (ROOT / "deploy/railway/entrypoint.sh").read_text(encoding="utf-8")
        at = entrypoint.index("sync-reply-gates.py")
        self.assertLess(entrypoint.index('touch "$upgrade_marker.done"'), at)
        self.assertLess(entrypoint.index("sync-public-prompt.py \\"), at)
        self.assertLess(at, entrypoint.index("wait-gateway-lease.py"))
        self.assertLess(at, entrypoint.index("exec setpriv --reuid=joseph --regid=joseph --init-groups python3 /usr/local/bin/gateway-standby.py"))
        self.assertIn('"$LIVE_SOURCE/config/openclaw.template.json"', entrypoint)
        self.assertIn("REPLIES_DISABLED", entrypoint)

        dockerfile = (ROOT / "deploy/railway/Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "COPY deploy/railway/sync-reply-gates.py /usr/local/bin/sync-reply-gates.py",
            dockerfile,
        )
        self.assertIn("/usr/local/bin/sync-reply-gates.py", dockerfile.split("RUN chmod 0755")[1])


if __name__ == "__main__":
    unittest.main()
