import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/railway/sync-public-prompt.py"
SPEC = importlib.util.spec_from_file_location("sync_public_prompt", SCRIPT)
SYNC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SYNC)


class PublicPromptSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.prompt = self.dir / "prompt.txt"
        self.config = self.dir / "openclaw.json"
        self.live = {
            "channels": {
                "whatsapp": {
                    "dmPolicy": "open",
                    "direct": {
                        "*": {"systemPrompt": "old prompt", "historyLimit": 20},
                        "+213000000000": {"systemPrompt": "peer override"},
                    },
                }
            },
            "plugins": {"entries": {"public-service-gate": {"config": {"greetingTemplatePath": "/x"}}}},
        }
        self.config.write_text(json.dumps(self.live), encoding="utf-8")
        self.config.chmod(0o600)

    def test_sets_only_the_wildcard_prompt(self) -> None:
        self.prompt.write_text("new prompt\n", encoding="utf-8")
        self.assertEqual(SYNC.sync(self.prompt, self.config), "updated")

        synced = json.loads(self.config.read_text(encoding="utf-8"))
        expected = json.loads(json.dumps(self.live))
        expected["channels"]["whatsapp"]["direct"]["*"]["systemPrompt"] = "new prompt"
        self.assertEqual(synced, expected)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_second_run_is_a_no_op(self) -> None:
        self.prompt.write_text("new prompt", encoding="utf-8")
        SYNC.sync(self.prompt, self.config)
        before = self.config.stat().st_mtime_ns
        self.assertEqual(SYNC.sync(self.prompt, self.config), "unchanged")
        self.assertEqual(self.config.stat().st_mtime_ns, before)

    def test_creates_the_wildcard_entry_when_missing(self) -> None:
        self.config.write_text(json.dumps({"channels": {"whatsapp": {}}}), encoding="utf-8")
        self.prompt.write_text("new prompt", encoding="utf-8")
        SYNC.sync(self.prompt, self.config)
        synced = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(synced["channels"]["whatsapp"]["direct"]["*"]["systemPrompt"], "new prompt")

    def test_bad_input_never_fails_the_boot_and_leaves_config_alone(self) -> None:
        original = self.config.read_text(encoding="utf-8")
        for prompt_text in ("", "   \n"):
            self.prompt.write_text(prompt_text, encoding="utf-8")
            run = subprocess.run(
                [sys.executable, str(SCRIPT), str(self.prompt), str(self.config)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(run.returncode, 0)
            self.assertIn("NOT synced", run.stderr)
            self.assertEqual(self.config.read_text(encoding="utf-8"), original)

        missing = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.dir / "missing.txt"), str(self.config)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(missing.returncode, 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)

    def test_tracked_prompt_syncs_and_entrypoint_runs_it_before_the_gateway(self) -> None:
        tracked = ROOT / "workspace/docs/public-whatsapp-system-prompt.txt"
        self.assertEqual(SYNC.sync(tracked, self.config), "updated")

        entrypoint = (ROOT / "deploy/railway/entrypoint.sh").read_text(encoding="utf-8")
        sync_at = entrypoint.index("sync-public-prompt.py")
        self.assertLess(entrypoint.index('touch "$upgrade_marker.done"'), sync_at)
        self.assertLess(sync_at, entrypoint.index("exec setpriv --reuid=joseph --regid=joseph --init-groups python3 /usr/local/bin/gateway-standby.py"))
        dockerfile = (ROOT / "deploy/railway/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY deploy/railway/sync-public-prompt.py /usr/local/bin/sync-public-prompt.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
