import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("console_api", ROOT / "deploy/railway/console-api.py")
API = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(API)
TOKEN, GATE = "t" * 32, "g" * 32


class CustomerBotHttpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.docs = self.state / "workspace" / "docs"
        self.docs.mkdir(parents=True)
        for name in ("public-whatsapp-reply-instructions.txt", "business-info.md"):
            shutil.copyfile(ROOT / "workspace" / "docs" / name, self.docs / name)
        env = patch.dict(os.environ, {"OPENCLAW_STATE_DIR": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        quiet = patch.object(API, "log")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.serve()

    def serve(self):
        server = API.build_server("127.0.0.1", 0, TOKEN, "/no-bridge", gate_token=GATE)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}/internal/console/customer-bot"

    def request(self, payload=None, token=TOKEN):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(self.url, data=data, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            result = urlopen(request, timeout=4)
        except HTTPError as error:
            result = error
        with result:
            return result.status, json.load(result), result.headers["Cache-Control"]

    def test_default_publish_no_op_and_overlay_survival(self):
        status, base, cache = self.request()
        self.assertEqual((status, cache), (200, "no-store"))
        self.assertIsNone(base["publishedAt"])
        self.assertIn("How to reply:", base["instructions"])
        self.assertIn("$95 per 1000", base["knowledge"])
        custom = self.state / "my-business.md"
        custom.write_text("Custom active facts", encoding="utf-8")
        config = {"plugins": {"entries": {"public-service-gate": {"config": {"businessInfoPath": str(custom)}}}}}
        (self.state / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
        customized = self.request()[1]
        self.assertNotEqual(customized["revision"], base["revision"])
        self.assertEqual(customized["knowledge"], "Custom active facts")

        update = {"instructions": "Start with the answer.", "knowledge": "Bricks: $107 per 1000.",
                  "expectedRevision": customized["revision"], "actor": "Fixture operator"}
        status, published, _ = self.request(update)
        self.assertEqual(status, 200)
        self.assertNotEqual(published["revision"], customized["revision"])
        self.assertEqual(published["publishedBy"], "Fixture operator")
        self.assertIsNotNone(published["publishedAt"])
        self.assertEqual(self.request(update)[1], published)  # retry after a lost response
        shutil.rmtree(self.state / "workspace")  # replace the baked source overlay
        custom.unlink()
        self.serve()  # a new API instance reads the same volume, with no in-memory state
        self.assertEqual(self.request()[1], published)
        self.assertEqual(API.CustomerBot(self.state).path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_publish_rejects_a_stale_revision(self):
        base = self.request()[1]
        changes = [{"instructions": label, "knowledge": label, "expectedRevision": base["revision"], "actor": "Fixture operator"}
                   for label in ("first", "second")]
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(self.request, changes))
        self.assertEqual(sorted(row[0] for row in results), [200, 409])
        current = self.request()[1]
        self.assertEqual(current, next(row[1]["current"] for row in results if row[0] == 409))
        self.assertEqual(current["instructions"], current["knowledge"])

    def test_invalid_or_unauthorized_requests_do_not_publish(self):
        for token in (GATE, "bad"):
            self.assertEqual(self.request(token=token)[0], 401)
            self.assertEqual(self.request({}, token=token)[0], 401)
        base = self.request()[1]
        update = {"instructions": "New", "knowledge": "New", "expectedRevision": base["revision"], "actor": "Operator"}
        for invalid in (" ", "nul\0byte", "é" * 8193, "\ud800", None):
            self.assertEqual(self.request({**update, "knowledge": invalid})[0], 400)
        self.assertEqual(self.request({**update, "knowledge": "x" * (256 * 1024)})[0], 400)
        self.assertEqual(self.request()[1], base)
        self.assertFalse(API.CustomerBot(self.state).path.exists())

    def test_corrupt_copy_is_not_mistaken_for_shipped_facts(self):
        store = API.CustomerBot(self.state)
        base = self.request()[1]
        store.directory.mkdir(parents=True)
        store.path.write_text("{", encoding="utf-8")
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.request({"instructions": "New", "knowledge": "New", "expectedRevision": base["revision"], "actor": "Operator"})[0], 503)
        self.assertEqual(store.path.read_text(encoding="utf-8"), "{")


if __name__ == "__main__":
    unittest.main()
