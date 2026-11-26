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

    def test_the_volume_root_itself_changes_hands(self):
        """Every path in the sweep below is chowned only `if [ -e "$path" ]`, and
        the dirs OpenClaw makes for itself -- state/, credentials/, identity/ --
        are created by nobody. A brand new Railway volume arrives owned by root,
        so until its mount point changes hands nothing running as joseph can
        create any of them, and the first boot crash-loops. Not the recursive
        form: the tree below is covered path by path, and a whole-volume sweep on
        every boot of the live gateway is a different thing entirely."""
        chown = ENTRYPOINT.split("# 3. Make mutable volume paths owned by joseph", 1)[1]
        self.assertIn('\nchown joseph:joseph "$OC_DIR"\n', chown)
        self.assertNotIn('chown -R joseph:joseph "$OC_DIR"\n', chown)

    def test_the_public_agents_store_is_not_written_before_doctor_sees_it(self):
        """The model-auth step creates the public agent's own OpenClaw database,
        and on a volume with no state that file does not exist yet. Written
        before the upgrade migration, it left doctor an agent store nothing had
        registered -- doctor reported `agent-deletion-journal
        (unverified-agent-databases)`, did not converge in three passes and hit
        the FATAL below it, crash-looping the boot. Doctor still runs before the
        gateway, so the key lands before the auth store is locked."""
        self.assertLess(
            ENTRYPOINT.index("running doctor --fix"),
            ENTRYPOINT.index("# Secondary agents do not inherit"),
        )
        self.assertLess(
            ENTRYPOINT.index("# Secondary agents do not inherit"),
            ENTRYPOINT.index("starting openclaw gateway on port"),
        )

    def test_nothing_pre_creates_the_public_agents_store(self):
        """Creating it as root ahead of time is the same mistake by another
        route: it hands doctor the unregistered store AND, because OpenClaw only
        chmods that directory when it had to create it, leaves it at mkdir's
        0755 for good."""
        self.assertNotIn('"$OC_DIR/agents/public/agent"', ENTRYPOINT)
        self.assertNotIn("agent_dir.mkdir", ENTRYPOINT)

    def test_the_model_key_only_goes_into_a_database_openclaw_made(self):
        """A database holding nothing but auth_profile_store is one OpenClaw
        rejects -- "uses schema version 0 ... Sessions remain unavailable" --
        so creating it here left a new bot's public agent permanently broken
        until someone ran doctor by hand. The gateway creates it correctly
        during its own startup, so the one boot that has none defers the key to
        the next boot instead of writing a database OpenClaw will not take."""
        self.assertIn('if [ ! -f "$public_agent_db" ]; then', ENTRYPOINT)
        self.assertLess(
            ENTRYPOINT.index('if [ ! -f "$public_agent_db" ]; then'),
            ENTRYPOINT.index("PUBLIC_MODEL_API_KEY="),
        )

    def test_the_agent_database_is_not_assumed_to_hold_openclaws_schema(self):
        """On a bot with no state that database is created by this step, so
        schema_meta does not exist yet -- OpenClaw's own ensureAgentSchema makes
        it and stamps the agent id when the gateway first opens it. Updating it
        regardless is what raised "no such table: schema_meta"."""
        self.assertIn("name = 'schema_meta'", ENTRYPOINT)

    def test_model_authentication_cannot_crashloop_a_boot(self):
        """A bot that boots without its public agent's model key can be fixed by
        the next boot, which runs this again. A bot that crash-loops cannot be
        fixed at all -- it never finishes starting, so the console reports only
        that it would not start."""
        self.assertIn(
            "WARNING: could not provision public agent model authentication",
            ENTRYPOINT,
        )


if __name__ == "__main__":
    unittest.main()
