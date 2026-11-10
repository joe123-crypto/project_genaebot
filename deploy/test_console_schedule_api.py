"""Real authenticated HTTP boundary for schedule writes; no production services."""
import json
import threading
import unittest
from test_console_write_api import Harness, API, ECHO_WRITE


class ScheduleAPI(Harness):
    def serve_schedule(self):
        base = self.serve()
        self.server.console["schedules"] = API.WriteBridge(self.script("schedule.py", ECHO_WRITE))
        self.server.console["reconcileWake"] = threading.Event()
        return base

    def test_authentication_and_write_key_are_required(self):
        base = self.serve_schedule()
        self.assertEqual(self.post(base, "/schedule", {}, token=None)[0], 401)
        self.set_key(None)
        self.assertEqual(self.post(base, "/schedule", {})[0], 503)
        self.assertEqual(self.server.console["schedules"].runs, 0)

    def test_forwards_request_invalidates_reads_and_wakes_worker(self):
        base = self.serve_schedule()
        self.server.console["bridge"].cache["stale"] = (99999999999, b"cached")
        data = {"date": "2026-11-10", "timezone": "Africa/Harare", "changes": [{"slug": "borehole-drilling", "time": "07:10", "expectedRevision": "a" * 64}], "actor": "Operator"}
        code, body = self.post(base, "/schedule", data)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["echo"], data)
        self.assertEqual(self.server.console["bridge"].cache, {})
        self.assertTrue(self.server.console["reconcileWake"].is_set())

    def test_rejects_worker_commands_bad_json_and_oversized_requests(self):
        base = self.serve_schedule()
        for body in [{"reconcile": True}, {"delete": "demo"}, [], b"{", b" " * (60 * 1024 + 1)]:
            self.assertEqual(self.post(base, "/schedule", body)[0], 400)
        self.assertEqual(self.server.console["schedules"].runs, 0)
        self.assertEqual(self.post(base, "/schedule?unexpected=1", {})[0], 400)

    def test_schedule_bridge_serializes_with_other_campaign_writes(self):
        self.serve()
        self.assertIs(self.server.console["schedules"].lock, self.server.console["writes"].lock)
        self.assertIs(self.server.console["campaigns"].lock, self.server.console["writes"].lock)


if __name__ == "__main__":
    unittest.main()
