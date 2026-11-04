"""The CLI helper must use the gateway user and preserve arguments and auth."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class GatewayExecTests(unittest.TestCase):
    def test_openclaw_prefix_uses_joseph_and_preserves_cli_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "capture"
            capture.write_text(
                "#!/usr/bin/env python3\nimport json,os,sys\n"
                "print(json.dumps(sys.argv[1:]))\n"
            )
            capture.chmod(0o755)
            cli = root / "openclaw"
            cli.write_text(
                "#!/usr/bin/env python3\nimport json,os,sys\n"
                "print(json.dumps(dict(argv=sys.argv[1:],home=os.environ['HOME'],"
                "old_home='OPENCLAW_HOME' in os.environ,token=os.environ['OPENCLAW_GATEWAY_TOKEN'])))\n"
            )
            cli.chmod(0o755)
            args = ["message", "send", "--account", "ads", "--message", 'Price $5; "hello" *']
            for mode in ("railway", "docker"):
                with self.subTest(mode=mode):
                    environment = {
                        **os.environ, "OPENCLAW_EXEC_MODE": mode,
                        "RAILWAY": str(capture), "DOCKER": str(capture),
                        "RAILWAY_PROJECT": "project", "RAILWAY_SERVICE": "gateway",
                        "RAILWAY_ENVIRONMENT": "production", "OPENCLAW_CONTAINER": "gateway-test",
                        "OPENCLAW_HOME": "/wrong/state", "HOME": "/root",
                        "GATEWAY_AUTH_TOKEN": "fixture token $value",
                        "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    }
                    result = subprocess.run(
                        ["bash", "-c", 'source "$1"; shift; "${GATEWAY_OPENCLAW[@]}" "$@"',
                         "test", str(ROOT / "deploy/gateway-exec.sh"), *args],
                        env=environment, text=True, capture_output=True, check=True,
                    )
                    command = json.loads(result.stdout)
                    shell = command.index("sh")
                    if mode == "railway":
                        self.assertEqual(command[:shell], [
                            "ssh", "--project", "project", "--service", "gateway",
                            "--environment", "production", "--", "runuser", "-u", "joseph", "--",
                        ])
                    else:
                        self.assertEqual(command[:shell], ["exec", "-u", "joseph", "gateway-test"])
                    self.assertEqual(command[shell + 1], "-lc")
                    # Evaluate the remote payload without a login shell replacing
                    # our fake executable PATH. Never contact a real gateway.
                    executed = subprocess.run(
                        ["/bin/sh", "-c", *command[shell + 2:]],
                        env=environment, text=True, capture_output=True, check=True,
                    )
                    data = json.loads(executed.stdout)
                    self.assertEqual(data, {
                        "argv": args, "home": "/home/joseph", "old_home": False,
                        "token": "fixture token $value",
                    })


if __name__ == "__main__":
    unittest.main()
