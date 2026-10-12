import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PluginConfigSchemaTests(unittest.TestCase):
    def test_local_plugin_template_keys_are_declared_by_their_schemas(self) -> None:
        template = json.loads((ROOT / "config/openclaw.template.json").read_text(encoding="utf-8"))
        plugin_entries = template.get("plugins", {}).get("entries", {})
        load_paths = template.get("plugins", {}).get("load", {}).get("paths", [])

        checked = []
        for load_path in load_paths:
            plugin_name = Path(load_path).name
            manifest_path = ROOT / "workspace/plugins" / plugin_name / "openclaw.plugin.json"
            self.assertTrue(manifest_path.is_file(), f"missing local plugin manifest: {manifest_path}")

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            schema = manifest.get("configSchema", {})
            if schema.get("additionalProperties") is not False:
                continue

            configured = plugin_entries.get(plugin_name, {}).get("config", {})
            declared = schema.get("properties", {})
            undeclared = sorted(set(configured) - set(declared))
            self.assertEqual(
                undeclared,
                [],
                f"{plugin_name} template config contains keys rejected by its schema",
            )
            checked.append(plugin_name)

        self.assertGreater(len(checked), 0, "no strict local plugin schemas were checked")

    def test_job_scout_onboarding_flag_is_boolean(self) -> None:
        manifest = json.loads(
            (ROOT / "workspace/plugins/public-service-gate/openclaw.plugin.json").read_text(encoding="utf-8")
        )

        flag_schema = manifest["configSchema"]["properties"]["jobScoutOnboardingEnabled"]
        self.assertEqual(flag_schema, {"type": "boolean"})

    def test_public_service_disable_flags_are_booleans(self) -> None:
        manifest = json.loads(
            (ROOT / "workspace/plugins/public-service-gate/openclaw.plugin.json").read_text(encoding="utf-8")
        )

        properties = manifest["configSchema"]["properties"]
        for flag in ["cvMakerEnabled", "memberGateEnabled"]:
            self.assertEqual(properties[flag], {"type": "boolean"})


if __name__ == "__main__":
    unittest.main()
