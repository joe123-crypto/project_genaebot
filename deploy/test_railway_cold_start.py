"""Pin how a bot with no state of its own boots.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.

A workspace's bot is created by the console with a brand new, empty Railway
volume, and nothing can put a file on a volume before its first boot. So the
entrypoint has to be able to start from the baked config template. What these
pin is that it does -- and that it still refuses to, for the bot that has real
state, because that refusal is what catches an unmounted or mis-mounted volume.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = (ROOT / "deploy/railway/entrypoint.sh").read_text(encoding="utf-8")
STAGE = (ROOT / "deploy/stage-openclaw-source.sh").read_text(encoding="utf-8")

TEMPLATE = "config/openclaw.template.json"
FLAG = "GENAIE_COLD_START"


class ColdStartTests(unittest.TestCase):
    def test_an_unseeded_volume_still_stops_the_boot(self):
        """The guard is the only thing standing between a mis-mounted volume and
        a bot quietly starting over on top of the state it could not see."""
        self.assertIn('FATAL: $OC_DIR/openclaw.json not found.', ENTRYPOINT)
        self.assertIn("exit 78", ENTRYPOINT)

    def test_cold_start_is_opt_in(self):
        self.assertIn('[ "${%s:-0}" = "1" ]' % FLAG, ENTRYPOINT)

    def test_cold_start_is_not_inferred_from_the_workspace_id(self):
        """Every workspace's bot names its workspace, the one with real state
        included, so inferring it from there would retire the guard above. The
        comment explaining that says the name, so only the code is read here."""
        guard = ENTRYPOINT.split("# 1. The volume must be seeded", 1)[1].split("\nfi\n", 1)[0]
        code = "\n".join(line for line in guard.splitlines() if not line.lstrip().startswith("#"))
        self.assertIn(FLAG, code)
        self.assertNotIn("GENAIE_WORKSPACE_ID", code)

    def test_it_starts_from_the_baked_template_and_checks_it_is_there(self):
        """An absent template must fall through to the refusal, not install
        nothing and let the gateway start on an empty config."""
        self.assertIn('[ -f "$LIVE_SOURCE/%s" ]' % TEMPLATE, ENTRYPOINT)
        self.assertIn('install -m 600 "$LIVE_SOURCE/%s" "$OC_DIR/openclaw.json"' % TEMPLATE, ENTRYPOINT)

    def test_the_image_carries_that_template(self):
        """The install reads it out of the baked overlay, so staging it is what
        makes a cold start possible at all."""
        self.assertIn(TEMPLATE, STAGE)

    def test_the_config_is_installed_before_anything_owns_or_reads_it(self):
        """It is written as root, so it has to land before the step that hands
        the volume to joseph -- and before .env, which is synthesized from this
        service's variables and is where the flag's own siblings arrive."""
        installed = ENTRYPOINT.index('install -m 600 "$LIVE_SOURCE/%s"' % TEMPLATE)
        self.assertLess(installed, ENTRYPOINT.index("# 2. Synthesize .env"))
        self.assertLess(installed, ENTRYPOINT.index("# 3. Make mutable volume paths owned by joseph"))

    def test_the_new_config_is_chowned_like_a_seeded_one(self):
        chown = ENTRYPOINT.split("# 3. Make mutable volume paths owned by joseph", 1)[1]
        self.assertIn('"$OC_DIR/openclaw.json"', chown.split("\ndone", 1)[0])


if __name__ == "__main__":
    unittest.main()
