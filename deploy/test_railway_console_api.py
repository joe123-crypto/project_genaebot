"""Pin how the console API is wired into the production container.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.

The wiring, not the server, is what can hurt the bot: the launch has to be
backgrounded, gated off by default, and ordered so that nothing it does can
abort the boot or delay the gateway.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = (ROOT / "deploy/railway/entrypoint.sh").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "deploy/railway/Dockerfile").read_text(encoding="utf-8")
FILTER = (ROOT / "deploy/stage-workspace.rsync").read_text(encoding="utf-8")

GATEWAY_EXEC = ("exec setpriv --reuid=joseph --regid=joseph --init-groups "
                "python3 /usr/local/bin/gateway-standby.py")


class ConsoleApiWiringTests(unittest.TestCase):
    def test_the_image_carries_the_server_and_its_bridge(self):
        self.assertIn(
            "COPY deploy/railway/console-api.py /usr/local/bin/console-api.py",
            DOCKERFILE)
        self.assertTrue((ROOT / "workspace/bin/console-read").is_file())
        # workspace/bin ships wholesale, so the bridge needs no filter entry.
        self.assertIn("+ /bin/***", FILTER)

    def test_the_declared_cron_manifest_is_baked_in(self):
        """console-read reads deploy/promo-crons.json, and stage-openclaw-source.sh
        never stages deploy/, so without this the campaign routes would 502 in
        production while working perfectly from a checkout."""
        self.assertIn(
            "COPY deploy/promo-crons.json /usr/local/lib/genaie/promo-crons.json",
            DOCKERFILE)

    def test_the_token_reaches_the_server_through_the_sanitized_env(self):
        secrets = ENTRYPOINT.split("SECRET_KEYS=(", 1)[1].split(")", 1)[0]
        self.assertIn("CONSOLE_API_TOKEN", secrets.split())

    def test_the_launch_is_off_unless_explicitly_enabled(self):
        self.assertIn('if [ "${GENAIE_CONSOLE_API:-0}" = "1" ]; then', ENTRYPOINT)

    def test_the_launch_is_backgrounded_and_never_waited_on(self):
        line = next(l.strip() for l in ENTRYPOINT.splitlines()
                    if "/usr/local/bin/console-api.py" in l)
        self.assertEqual(
            line, "runuser -u joseph -- python3 /usr/local/bin/console-api.py &")
        self.assertNotIn("wait", line)

    def test_the_launch_cannot_delay_or_abort_the_gateway(self):
        launch = ENTRYPOINT.index("/usr/local/bin/console-api.py")
        # After the ownership sweep, so nothing it creates is left owned by root.
        self.assertLess(ENTRYPOINT.rindex("chown -R joseph:joseph"), launch)
        # Before the readiness probe and the exec, both of which must still run.
        self.assertLess(launch, ENTRYPOINT.index("/usr/local/bin/report-release-ready.py"))
        self.assertLess(launch, ENTRYPOINT.index(GATEWAY_EXEC))

    def test_the_gateway_exec_is_untouched(self):
        self.assertIn(GATEWAY_EXEC, ENTRYPOINT)
        self.assertIn("-- openclaw gateway --port \"$PORT\" --bind lan --tailscale off",
                      ENTRYPOINT)

    def test_the_server_does_not_take_the_gateway_port(self):
        """PORT is reassigned early in the entrypoint, so every child sees
        18789. The console API must name its own port or it would collide."""
        server = (ROOT / "deploy/railway/console-api.py").read_text(encoding="utf-8")
        self.assertIn("CONSOLE_API_PORT", server)
        self.assertNotIn('environ.get("PORT")', server)
        self.assertNotIn("environ['PORT']", server)
        self.assertNotEqual(ROOT, None)

    def test_enabling_it_needs_no_code_change(self):
        """Everything the operator has to set is read from the environment."""
        for name in ("GENAIE_CONSOLE_API", "CONSOLE_API_TOKEN"):
            self.assertIn(name, ENTRYPOINT, name)


if __name__ == "__main__":
    unittest.main()
