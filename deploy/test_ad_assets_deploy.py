"""The deployment wiring around the R2 move: the switch stays the operator's,
no credential for writing R2 goes anywhere near the container, and nothing in
production pins a variant image the index cannot express.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import importlib.machinery
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "workspace/lib"), str(ROOT / "deploy")):
    if path not in sys.path:
        sys.path.insert(0, path)

import ad_assets  # noqa: E402


def upload_module():
    loader = importlib.machinery.SourceFileLoader(
        "upload_ad_image", str(ROOT / "deploy" / "upload-ad-image.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


TOOL = upload_module()

ENTRYPOINT = (ROOT / "deploy/railway/entrypoint.sh").read_text(encoding="utf-8")
RELEASE = (ROOT / "deploy/railway-release.py").read_text(encoding="utf-8")
UPLOAD = (ROOT / "deploy/upload-ad-image.py").read_text(encoding="utf-8")
SIGNER = (ROOT / "deploy/r2_sigv4.py").read_text(encoding="utf-8")
MODULE = (ROOT / "workspace/lib/ad_assets.py").read_text(encoding="utf-8")
CRON = ROOT / "workspace/cron"
R2_WRITE_VARS = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ACCOUNT_ID",
                 "R2_BUCKET", "R2_ENDPOINT")


class SwitchTest(unittest.TestCase):
    def test_releases_neither_set_nor_clear_the_asset_source(self):
        # Like GENAIE_CONFIG_SOURCE: an operator switch, not a release setting.
        # A release profile that owned it would silently flip the images back.
        owned = RELEASE.split("OWNED = (", 1)[1].split(")", 1)[0]
        for name in (ad_assets.MODE_VAR, ad_assets.BASE_VAR, ad_assets.DIR_VAR):
            self.assertNotIn(name, owned)

    def test_the_switch_does_not_need_the_sanitised_secret_env(self):
        # GENAIE_CONFIG_SOURCE is not in SECRET_KEYS either: Railway service
        # variables are already in the gateway's environment and crons inherit
        # it. SECRET_KEYS exists for values openclaw's own config interpolates.
        secrets = ENTRYPOINT.split("SECRET_KEYS=(", 1)[1].split(")", 1)[0].split()
        self.assertNotIn("GENAIE_CONFIG_SOURCE", secrets)
        self.assertNotIn(ad_assets.MODE_VAR, secrets)

    def test_the_index_read_uses_the_key_the_runtime_already_has(self):
        self.assertIn("GCP_SERVICE_ACCOUNT_B64",
                      ENTRYPOINT.split("SECRET_KEYS=(", 1)[1].split(")", 1)[0].split())


class CredentialTest(unittest.TestCase):
    def test_r2_write_credentials_reach_only_the_console_api_child(self):
        for name in R2_WRITE_VARS:
            self.assertNotIn(name, RELEASE, name)
            self.assertNotIn(name, MODULE, name)
        self.assertIn("unset R2_ACCOUNT_ID R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY", ENTRYPOINT)
        self.assertLess(ENTRYPOINT.index("unset R2_ACCOUNT_ID"), ENTRYPOINT.index('if [ "${GENAIE_CONSOLE_API:-0}"'))
        self.assertIn('R2_ACCESS_KEY_ID="$console_r2_access" R2_SECRET_ACCESS_KEY="$console_r2_secret"', ENTRYPOINT)
        self.assertIn("unset console_r2_account console_r2_bucket console_r2_access console_r2_secret", ENTRYPOINT)

    def test_the_runtime_module_never_signs_a_request(self):
        for forbidden in ("hmac", "Authorization", "AWS4", "r2_sigv4"):
            self.assertNotIn(forbidden, MODULE, forbidden)

    def test_the_signer_is_baked_separately_from_the_broadcast_workspace(self):
        # Broadcasts continue to use public GETs; the console API loads this helper.
        staged = (ROOT / "deploy/stage-workspace.rsync").read_text(encoding="utf-8")
        self.assertNotIn("r2_sigv4", staged)
        self.assertTrue((ROOT / "deploy/r2_sigv4.py").is_file())
        self.assertFalse((ROOT / "workspace/lib/r2_sigv4.py").exists())
        self.assertIn("COPY deploy/r2_sigv4.py /usr/local/lib/genaie/r2_sigv4.py", (ROOT / "deploy/railway/Dockerfile").read_text())

    def test_the_upload_script_names_its_firestore_key_explicitly(self):
        # Never pick up the read-only runtime key by accident, the way
        # seed-firestore.py already refuses to.
        self.assertIn('env.pop("GCP_SERVICE_ACCOUNT_B64", None)', UPLOAD)

    def test_the_payload_digest_is_always_signed(self):
        self.assertIn("x-amz-content-sha256", SIGNER)
        self.assertNotIn("UNSIGNED-PAYLOAD", SIGNER.split('"""', 2)[-1])


class VariantImageTest(unittest.TestCase):
    def test_no_production_post_script_pins_a_variant_image(self):
        # resolve() maps a pinned variant image by filename, which cannot express
        # "add a picture to the rotation". Nothing uses it today; the day a
        # campaign does, this fails and whoever added it has to index it.
        pinned = []
        for script in sorted(CRON.glob("*-promo/post*.py")):
            body = script.read_text(encoding="utf-8")
            if re.search(r"^\s*(VARIANT_IMAGES|variant_images)\s*=", body, re.M):
                pinned.append(script.relative_to(ROOT))
        self.assertEqual(pinned, [])

    def test_every_post_script_still_has_an_image_override(self):
        # is_baked() only substitutes a path inside the campaign's own assets
        # directory, which is what keeps these overrides working in r2 mode.
        for directory in sorted(CRON.glob("*-promo")):
            scripts = sorted(directory.glob("post*.py"))
            self.assertTrue(scripts, directory)
            body = "".join(s.read_text(encoding="utf-8") for s in scripts)
            # The call is spelled across one or two lines depending on the
            # script, so match the opening of it rather than the whole call.
            self.assertTrue(
                re.search(r'os\.environ\.get\(\s*"[A-Z_]*IMAGE"', body),
                "%s declares no *_IMAGE override" % directory.name)


class CredentialFileTest(unittest.TestCase):
    """Where the operator's R2 and Firestore credentials are read from."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "r2-upload.env")

    def write(self, text, mode=0o600):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(self.path, mode)
        return self.path

    def test_the_default_path_is_outside_every_checkout(self):
        # deploy/** is un-ignored wholesale, so a credentials file in the repo
        # would be staged by `git add -A`, and ci/check.py only refuses tracked
        # names BEGINNING with ".env" -- so "deploy/r2.env" would slip through.
        self.assertNotIn(str(ROOT), TOOL.DEFAULT_ENV_FILE)
        self.assertTrue(TOOL.DEFAULT_ENV_FILE.endswith("genaie/r2-upload.env"))

    def test_a_file_readable_by_other_accounts_is_refused(self):
        for mode in (0o644, 0o640, 0o604):
            self.write("R2_BUCKET=ads\n", mode)
            with self.assertRaisesRegex(TOOL.UploadError, "readable by other"):
                TOOL.load_env_file(self.path, required=True, env={})

    def test_comments_blank_lines_export_and_quotes_are_handled(self):
        values = TOOL.parse_env_file(
            '# a comment\n\n'
            'R2_BUCKET=genaie-ads\n'
            'export R2_ACCOUNT_ID=abc123\n'
            'R2_SECRET_ACCESS_KEY="s3cr3t"\n'
            "R2_ACCESS_KEY_ID='keyid'\n")
        self.assertEqual(values, {"R2_BUCKET": "genaie-ads",
                                  "R2_ACCOUNT_ID": "abc123",
                                  "R2_SECRET_ACCESS_KEY": "s3cr3t",
                                  "R2_ACCESS_KEY_ID": "keyid"})

    def test_it_is_not_a_shell(self):
        # A file of credentials must not be able to run anything or interpolate.
        values = TOOL.parse_env_file("R2_BUCKET=$(whoami)\nR2_ACCOUNT_ID=${HOME}\n")
        self.assertEqual(values["R2_BUCKET"], "$(whoami)")
        self.assertEqual(values["R2_ACCOUNT_ID"], "${HOME}")

    def test_a_malformed_line_names_its_number(self):
        self.write("R2_BUCKET=ads\nthis is not an assignment\n")
        with self.assertRaisesRegex(TOOL.UploadError, "line 2"):
            TOOL.load_env_file(self.path, required=True, env={})

    def test_the_environment_wins_over_the_file(self):
        self.write("R2_BUCKET=from-file\nR2_ACCOUNT_ID=from-file\n")
        env = {"R2_BUCKET": "from-the-shell"}
        TOOL.load_env_file(self.path, required=True, env=env)
        self.assertEqual(env["R2_BUCKET"], "from-the-shell")
        self.assertEqual(env["R2_ACCOUNT_ID"], "from-file")

    def test_a_missing_default_file_is_not_an_error(self):
        self.assertIsNone(TOOL.load_env_file(
            os.path.join(self.dir, "absent.env"), required=False, env={}))

    def test_a_missing_named_file_is_an_error(self):
        with self.assertRaisesRegex(TOOL.UploadError, "no such env file"):
            TOOL.load_env_file(os.path.join(self.dir, "absent.env"),
                               required=True, env={})

    def test_no_credential_value_is_committed_anywhere_in_the_repo(self):
        # The template the operator fills in lives outside the repo; nothing
        # here may carry a real key.
        for name in R2_WRITE_VARS:
            for text in (UPLOAD, SIGNER, MODULE):
                for line in text.splitlines():
                    if "%s=" % name in line:
                        self.fail("%s has a value assigned: %r" % (name, line))


class R2TargetTest(unittest.TestCase):
    def test_the_account_id_is_shortened_in_the_log_line(self):
        account = "0123456789abcdef0123456789abcdef"
        line = TOOL.r2_target("https://%s.r2.cloudflarestorage.com" % account, "ads")
        self.assertNotIn(account, line)
        self.assertTrue(line.startswith("012345...ef."))
        self.assertTrue(line.endswith("r2.cloudflarestorage.com/ads"))

    def test_a_short_host_is_left_alone(self):
        self.assertEqual(TOOL.r2_target("https://r2.example", "ads"),
                         "r2.example/ads")


class ImageLimitTest(unittest.TestCase):
    def test_the_cap_matches_what_the_gateway_enforces(self):
        self.assertEqual(ad_assets.MAX_BYTES, 6 * 1024 * 1024)

    def test_the_cache_is_inside_the_gateways_media_roots(self):
        # openclaw reads outbound local media only from an allowlist that
        # includes <state>/media and does NOT include <state>/runtime.
        self.assertEqual(ad_assets.CACHE_SUBDIR, "media/ad-assets")
        self.assertTrue(ad_assets.cache_dir({"OPENCLAW_STATE_DIR": "/s"})
                        .startswith("/s/media/"))

    def test_the_cache_is_outside_the_tree_the_entrypoint_wipes(self):
        # entrypoint.sh rsyncs --delete over workspace/ on every boot.
        self.assertNotIn("workspace", ad_assets.CACHE_SUBDIR)
        self.assertIn('rsync -a --delete', ENTRYPOINT)


if __name__ == "__main__":
    unittest.main()
