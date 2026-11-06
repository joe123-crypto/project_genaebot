"""The deployment contract for operator-config writes.

These assertions are about text in the entrypoint and the release manager, not
behaviour, because that is where the guarantee lives: a campaign run must not be
able to rewrite the config it is about to act on, and the only thing keeping the
write key out of a cron run's environment is that it is absent from SECRET_KEYS
and unset before the gateway is exec'd. Nothing else would catch a well-meaning
edit that adds it to that list.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = (ROOT / "deploy/railway/entrypoint.sh").read_text(encoding="utf-8")
RELEASE = (ROOT / "deploy/railway-release.py").read_text(encoding="utf-8")
KEY = "GCP_SERVICE_ACCOUNT_WRITE_B64"


class WriteKeyIsolation(unittest.TestCase):
    def test_the_write_key_is_not_a_secret_key(self):
        # SECRET_KEYS is exported into the shell that execs the gateway, so
        # everything on it reaches every cron run.
        block = re.search(r"SECRET_KEYS=\((.*?)\)", ENTRYPOINT, re.S)
        self.assertIsNotNone(block, "SECRET_KEYS not found in the entrypoint")
        self.assertNotIn(KEY, block.group(1))

    def test_the_write_key_is_unset_before_the_gateway_starts(self):
        self.assertIn("unset %s" % KEY, ENTRYPOINT)
        unset_at = ENTRYPOINT.index("unset %s" % KEY)
        exec_at = ENTRYPOINT.rindex("setpriv")
        self.assertLess(unset_at, exec_at,
                        "the write key must be unset before the gateway is exec'd")

    def test_the_key_is_handed_only_to_the_console_api(self):
        # One assignment, on the console-api invocation itself.
        uses = [line.strip() for line in ENTRYPOINT.splitlines()
                if "%s=" % KEY in line and not line.strip().startswith("#")]
        self.assertEqual(len(uses), 1, uses)
        self.assertIn('%s="$console_write_key"' % KEY, uses[0])
        self.assertIn("console-api.py", ENTRYPOINT[ENTRYPOINT.index(uses[0]):][:300])

    def test_the_write_key_is_not_release_managed(self):
        # A deploy must neither set nor clear it, so enabling writes stays a
        # deliberate, separate act.
        owned = re.search(r"OWNED\s*=\s*\((.*?)\)", RELEASE, re.S)
        self.assertIsNotNone(owned, "OWNED not found in railway-release.py")
        self.assertNotIn(KEY, owned.group(1))

    def test_the_console_api_still_launches_when_writes_are_off(self):
        # Reads must not depend on the write key being present.
        launch = ENTRYPOINT[ENTRYPOINT.index('GENAIE_CONSOLE_API:-0'):]
        launch = launch[:launch.index("report-release-ready.py")]
        self.assertIn("read-only: no write key", launch)


class DocumentedWhereItIsOperated(unittest.TestCase):
    def test_both_readmes_cover_the_write_surface(self):
        # The CI docs policy requires deploy/railway/README.md to change with the
        # entrypoint; this pins that the variable itself is actually named there.
        for path in ("README.md", "deploy/railway/README.md"):
            with self.subTest(path=path):
                self.assertIn(KEY, (ROOT / path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
