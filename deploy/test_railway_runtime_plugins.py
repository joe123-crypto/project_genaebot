import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/railway/sync-runtime-plugins.py"
SPEC = importlib.util.spec_from_file_location("runtime_plugins", SCRIPT)
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)


class RuntimePluginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.template = Path(self.directory.name) / "template.json"
        self.template.write_bytes((ROOT / "config/openclaw.template.json").read_bytes())
        self.config = Path(self.directory.name) / "live.json"
        self.live = {
            "plugins": {"entries": {
                "voice-call": {"enabled": True, "config": {"private": "retain"}},
                "whatsapp": {"enabled": True},
                "memory-core": {"enabled": True},
                "file-transfer": {"enabled": True},
                "linux-node": {"enabled": True},
                "openrouter": {"enabled": True},
                "openai": {"enabled": True},
                "vercel-ai-gateway": {"enabled": True},
                "public-service-gate": {"config": {"enabled": True}},
            }, "load": {"paths": ["live/path"]}},
            "channels": {"whatsapp": {"accounts": {
                "default": {"enabled": True}, "ads": {"enabled": True},
            }}},
            "bindings": [{"agentId": "main", "private": "retain"}],
        }
        self.config.write_text(json.dumps(self.live))
        self.config.chmod(0o600)

    def test_policy_preserves_required_plugins_accounts_and_private_config(self):
        SYNC.sync(self.template, self.config)
        actual = json.loads(self.config.read_text())
        expected = copy.deepcopy(self.live)
        for plugin in SYNC.GOVERNED_PLUGINS:
            expected["plugins"]["entries"].setdefault(plugin, {})["enabled"] = False
        self.assertEqual(actual, expected)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        before = self.config.stat().st_mtime_ns
        self.assertEqual(SYNC.sync(self.template, self.config), ["runtime plugins unchanged"])
        self.assertEqual(self.config.stat().st_mtime_ns, before)

    def test_dry_run_leaves_bytes_unchanged(self):
        before = self.config.read_bytes()
        self.assertIn("DRY RUN", SYNC.sync(self.template, self.config, True)[0])
        self.assertEqual(self.config.read_bytes(), before)

    def test_bad_policy_or_live_shape_never_partially_writes(self):
        original = self.config.read_bytes()
        policy = json.loads(self.template.read_text())
        policy["plugins"]["entries"][SYNC.GOVERNED_PLUGINS[-1]]["enabled"] = "false"
        self.template.write_text(json.dumps(policy))
        with self.assertRaises(ValueError):
            SYNC.sync(self.template, self.config)
        self.assertEqual(self.config.read_bytes(), original)
        self.template.write_bytes((ROOT / "config/openclaw.template.json").read_bytes())
        self.config.write_text('{"plugins":{"entries":[]}}')
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            SYNC.sync(self.template, self.config)
        self.assertEqual(self.config.read_bytes(), before)

    def test_template_can_restore_a_plugin_without_erasing_options(self):
        policy = json.loads(self.template.read_text())
        policy["plugins"]["entries"]["voice-call"]["enabled"] = True
        self.template.write_text(json.dumps(policy))
        SYNC.sync(self.template, self.config)
        self.assertEqual(json.loads(self.config.read_text())["plugins"]["entries"]["voice-call"],
                         self.live["plugins"]["entries"]["voice-call"])

    def test_boot_wires_sync_after_upgrade_and_before_validation(self):
        entrypoint = (ROOT / "deploy/railway/entrypoint.sh").read_text()
        position = entrypoint.index("python3 /usr/local/bin/sync-runtime-plugins.py")
        self.assertLess(entrypoint.index('touch "$upgrade_marker.done"'), position)
        self.assertLess(position, entrypoint.index("# Non-fatal on purpose:"))
        self.assertLess(position, entrypoint.index("exec setpriv --reuid=joseph --regid=joseph --init-groups python3 /usr/local/bin/gateway-standby.py"))
        self.assertIn("COPY deploy/railway/sync-runtime-plugins.py /usr/local/bin/sync-runtime-plugins.py",
                      (ROOT / "deploy/railway/Dockerfile").read_text())


if __name__ == "__main__":
    unittest.main()
