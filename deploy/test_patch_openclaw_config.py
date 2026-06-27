import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).with_name("patch-openclaw-config.py")
SPEC = importlib.util.spec_from_file_location("patch_openclaw_config", MODULE_PATH)
PATCHER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCHER)


class ApplyTemplateTests(unittest.TestCase):
    def test_tracked_prompt_replaces_wildcard_and_preserves_peer_overrides(self):
        live = {
            "channels": {
                "whatsapp": {
                    "direct": {
                        "*": {"systemPrompt": "old prompt", "historyLimit": 20},
                        "+213000000000": {"systemPrompt": "peer override"},
                    }
                }
            },
            "plugins": {
                "entries": {
                    "public-service-gate": {
                        "config": {"whatsappAdminUsers": ["+213000000000"]}
                    }
                }
            },
        }
        template = {
            "channels": {"whatsapp": {"dmPolicy": "open"}},
            "plugins": {"entries": {"public-service-gate": {"config": {}}}},
        }

        merged = PATCHER.apply_template(live, template, "  new tracked prompt\n")

        direct = merged["channels"]["whatsapp"]["direct"]
        self.assertEqual(direct["*"]["systemPrompt"], "new tracked prompt")
        self.assertEqual(direct["*"]["historyLimit"], 20)
        self.assertEqual(direct["+213000000000"]["systemPrompt"], "peer override")
        self.assertEqual(live["channels"]["whatsapp"]["direct"]["*"]["systemPrompt"], "old prompt")
        self.assertEqual(
            merged["plugins"]["entries"]["public-service-gate"]["config"]["whatsappAdminUsers"],
            ["+213000000000"],
        )

    def test_empty_prompt_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "prompt is empty"):
            PATCHER.apply_template({}, {}, "  \n")


if __name__ == "__main__":
    unittest.main()
