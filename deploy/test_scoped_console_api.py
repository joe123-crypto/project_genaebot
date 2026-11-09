"""New narrow read and atomic group-write routes retain the API's access gates."""
import json
import unittest
import test_console_api as reads
from test_console_write_api import Harness


class ScopedReadAPI(unittest.TestCase):
    setUp = reads.ConsoleApiTests.setUp
    write = reads.ConsoleApiTests.write
    serve = reads.ConsoleApiTests.serve
    fetch = reads.ConsoleApiTests.fetch
    def test_scoped_routes_forward_only_validated_arguments(self):
        base = self.serve()
        for route, args in [("/campaign/demo", ["campaign", "demo"]),
                            ("/campaign-state/demo", ["campaign-state", "demo"]),
                            ("/campaign-groups/demo", ["campaign-groups", "demo"]),
                            ("/captions/demo", ["captions", "demo"]),
                            ("/group/123%40g.us", ["group", "123@g.us"]),
                            ("/groups-by-id/123%40g.us%2C456%40g.us", ["groups-by-id", "123@g.us,456@g.us"]),
                            ("/capabilities", ["capabilities"]), ("/settings", ["settings"]),
                            ("/navigation", ["navigation"])]:
            code, body, _ = self.fetch(base + route)
            self.assertEqual(code, 200, route)
            self.assertEqual(json.loads(body)["args"], args)
            self.assertEqual(self.fetch(base + route, token=None)[0], 401)
        for route in ("/group/not-a-group", "/groups-by-id/123%40g.us%2Cbad", "/campaign/%2e%2e"):
            self.assertEqual(self.fetch(base + route)[0], 404)


class GroupBatchAPI(Harness):
    def test_batch_is_authenticated_bounded_and_invalidates_reads(self):
        base = self.serve()
        payload = {"changes": [{"id": "123@g.us", "fields": {"note": "test"}}], "actor": "Operator"}
        self.server.console["bridge"].cache["old"] = (999999999999, b"cached")
        code, body = self.post(base, "/config/batch/groups", payload)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["echo"], dict(payload, collection="groups"))
        self.assertEqual(self.server.console["bridge"].cache, {})
        self.assertEqual(self.post(base, "/config/batch/groups", payload, token=None)[0], 401)
        for invalid in ({"changes": []}, {"changes": [{}] * 101}, {"changes": [], "delete": True}, b" " * 65537):
            self.assertEqual(self.post(base, "/config/batch/groups", invalid)[0], 400)
        self.set_key(None)
        self.assertEqual(self.post(base, "/config/batch/groups", payload)[0], 503)


if __name__ == "__main__":
    unittest.main()
