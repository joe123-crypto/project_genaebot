"""Drive the real authenticated HTTP route with an isolated echo bridge."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from test_console_write_api import Harness, API, ECHO_WRITE


class CampaignAPI(Harness):
    def serve_campaign(self, source=ECHO_WRITE):
        base = self.serve()
        self.server.console["campaigns"] = API.WriteBridge(self.script("create.py", source))
        return base
    def test_forwards_request_and_clears_cached_reads(self):
        base = self.serve_campaign()
        self.server.console["bridge"].cache["stale"] = (99999999999, b"cached")
        code, body = self.post(base, "/campaigns/create", {"slug": "demo", "requestId": "request"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["echo"]["slug"], "demo")
        self.assertEqual(self.server.console["bridge"].cache, {})
    def test_authentication_and_write_key_are_required(self):
        base = self.serve_campaign()
        self.assertEqual(self.post(base, "/campaigns/create", {}, token=None)[0], 401)
        self.set_key(None)
        self.assertEqual(self.post(base, "/campaigns/create", {})[0], 503)
        self.assertEqual(self.server.console["campaigns"].runs, 0)
    def test_rejects_untrusted_worker_command_and_oversized_input(self):
        base = self.serve_campaign()
        for body in [{"reconcile": True}, [], b"{", b" " * (60 * 1024 + 1)]:
            self.assertEqual(self.post(base, "/campaigns/create", body)[0], 400)
        self.assertEqual(self.server.console["campaigns"].runs, 0)
    def test_pending_is_a_successful_saved_result(self):
        source = 'import json,sys\nsys.stdin.read()\njson.dump({"ok":True,"status":200,"slug":"demo","scheduling":"pending"},sys.stdout)'
        base = self.serve_campaign(source)
        code, body = self.post(base, "/campaigns/create", {"slug": "demo"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["scheduling"], "pending")


if __name__ == "__main__":
    unittest.main()
