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

    # Captured from the live openclaw.json on the Railway volume on 2026-10-12.
    # Nothing applies config/openclaw.template.json on Railway, so the live config
    # drifts from the template and is the set the shipped schema must actually
    # accept. Removing four of these from the schema crashlooped production:
    # configSchema is additionalProperties:false, so the live config stopped
    # validating and the gateway refused to start. Update this list only from a
    # fresh read of the live config, never to make a failing test pass.
    LIVE_PUBLIC_SERVICE_GATE_KEYS = [
        "enabled",
        "greetingTemplatePath",
        "botWhatsappNumber",
        "memberGateEnabled",
        "pendingAckText",
        "newsEnabled",
        "eventScoutEnabled",
        "jobScoutOnboardingEnabled",
        "cvMakerEnabled",
        "whatsappAdminUsers",
    ]

    def test_schema_accepts_every_key_in_the_live_production_config(self) -> None:
        manifest = json.loads(
            (ROOT / "workspace/plugins/public-service-gate/openclaw.plugin.json").read_text(encoding="utf-8")
        )
        schema = manifest["configSchema"]
        self.assertIs(schema.get("additionalProperties"), False)

        rejected = sorted(set(self.LIVE_PUBLIC_SERVICE_GATE_KEYS) - set(schema["properties"]))
        self.assertEqual(
            rejected,
            [],
            "these keys exist in the live config but are no longer declared, so the "
            "gateway will refuse to start: remove them from the live config first",
        )

    def test_retired_keys_stay_declared_and_typed(self) -> None:
        # Job Scout, account-link, news and events are gone, but the live config
        # still carries these keys, so the schema must keep accepting them.
        manifest = json.loads(
            (ROOT / "workspace/plugins/public-service-gate/openclaw.plugin.json").read_text(encoding="utf-8")
        )

        properties = manifest["configSchema"]["properties"]
        for key, kind in (
            ("jobScoutOnboardingEnabled", "boolean"),
            ("newsEnabled", "boolean"),
            ("eventScoutEnabled", "boolean"),
            ("pendingLinksPath", "string"),
        ):
            self.assertEqual(properties[key]["type"], kind)
            self.assertIn("Deprecated and unread", properties[key]["description"])

    def test_public_service_disable_flags_are_booleans(self) -> None:
        manifest = json.loads(
            (ROOT / "workspace/plugins/public-service-gate/openclaw.plugin.json").read_text(encoding="utf-8")
        )

        properties = manifest["configSchema"]["properties"]
        for flag in ["cvMakerEnabled", "memberGateEnabled"]:
            self.assertEqual(properties[flag], {"type": "boolean"})


if __name__ == "__main__":
    unittest.main()
