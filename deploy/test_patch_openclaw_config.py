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
        self.assertEqual(merged["bindings"][0]["agentId"], "main")
        self.assertEqual(merged["bindings"][0]["match"]["peer"]["id"], "+213000000000")
        self.assertEqual(direct["+213000000000"]["systemPrompt"], "peer override")

    def test_admin_prompt_is_added_without_overwriting_public_wildcard(self):
        live = {
            "plugins": {"entries": {"public-service-gate": {"config": {"whatsappAdminUsers": ["+213000000000"]}}}}
        }
        template = {
            "bindings": [{"agentId": "public", "match": {"channel": "whatsapp"}}],
            "plugins": {"entries": {"public-service-gate": {"config": {}}}},
        }

        merged = PATCHER.apply_template(live, template, "public prompt")

        direct = merged["channels"]["whatsapp"]["direct"]
        self.assertEqual(direct["*"]["systemPrompt"], "public prompt")
        self.assertIn("private administrator", direct["+213000000000"]["systemPrompt"])
        self.assertEqual([binding["agentId"] for binding in merged["bindings"]], ["main", "public"])

    def test_retired_meta_key_is_dropped_from_preserved_meta(self):
        live = {"meta": {"lastTouchedVersion": "2026.6.34", "lastTouchedAt": "2026-10-01T00:00:00.000Z"}}

        merged = PATCHER.apply_template(live, {}, "public prompt")

        self.assertEqual(merged["meta"], {"lastTouchedVersion": "2026.6.34"})

    def test_empty_prompt_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "prompt is empty"):
            PATCHER.apply_template({}, {}, "  \n")


if __name__ == "__main__":
    unittest.main()
