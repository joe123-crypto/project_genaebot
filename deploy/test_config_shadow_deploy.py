"""Shadow mode's deployment wiring: the key reaches cron scripts through the
sanitised environment, and releases leave both variables alone.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = (ROOT / "deploy/railway/entrypoint.sh").read_text(encoding="utf-8")
RELEASE = (ROOT / "deploy/railway-release.py").read_text(encoding="utf-8")


class ShadowDeployTest(unittest.TestCase):
    def test_the_key_goes_through_the_sanitised_secret_env(self):
        secrets = ENTRYPOINT.split("SECRET_KEYS=(", 1)[1].split(")", 1)[0]
        self.assertIn("GCP_SERVICE_ACCOUNT_B64", secrets.split())

    def test_releases_neither_set_nor_clear_shadow_mode(self):
        # Like GENAIE_CONSOLE_API: an operator switch, not a release setting. A
        # release profile that owned it would silently turn shadow mode off.
        owned = RELEASE.split("OWNED = (", 1)[1].split(")", 1)[0]
        for name in ("GENAIE_CONFIG_SOURCE", "GCP_SERVICE_ACCOUNT_B64"):
            self.assertNotIn(name, owned)


if __name__ == "__main__":
    unittest.main()
