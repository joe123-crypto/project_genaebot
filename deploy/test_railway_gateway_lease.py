import importlib.util
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/railway/wait-gateway-lease.py"
SPEC = importlib.util.spec_from_file_location("wait_gateway_lease", SCRIPT)
LEASE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(LEASE)


class FakeClock:
    def __init__(self) -> None:
        self.t = time.time()
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class GatewayLeaseWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Path(tempfile.mkdtemp()) / "openclaw.sqlite"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE state_leases (scope TEXT, lease_key TEXT, owner TEXT, expires_at INTEGER,"
            " heartbeat_at INTEGER, payload_json TEXT, created_at INTEGER, updated_at INTEGER,"
            " PRIMARY KEY (scope, lease_key))"
        )
        conn.commit()
        conn.close()
        self.logs: list[str] = []

    def put_lease(self, host: str, expires_in_s: float, now: float) -> None:
        payload = {"owner": {"pid": 42, "host": host, "startedAt": 1}, "port": 18789, "mode": "foreground"}
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT OR REPLACE INTO state_leases VALUES ('gateway-owner', 'global', 'o', ?, 0, ?, 0, 0)",
            (int((now + expires_in_s) * 1000), json.dumps(payload)),
        )
        conn.commit()
        conn.close()

    def run_wait(self, clock: FakeClock, max_wait: float = 330.0) -> str:
        # The real expiry check reads wall-clock time; keep the fake clock on it.
        LEASE.time.time = clock.now
        try:
            return LEASE.wait_for_lease(self.db, "new-host", max_wait, clock.now, clock.sleep, self.logs.append)
        finally:
            LEASE.time.time = time.time

    def test_no_database_or_table_starts_immediately(self) -> None:
        clock = FakeClock()
        LEASE.time.time = clock.now
        try:
            self.assertEqual(
                LEASE.wait_for_lease(self.db.parent / "missing.sqlite", "h", 330, clock.now, clock.sleep, self.logs.append),
                "clear",
            )
        finally:
            LEASE.time.time = time.time
        self.assertEqual(self.run_wait(clock), "clear")
        self.assertEqual(clock.sleeps, [])

    def test_stale_lease_from_killed_container_is_waited_out(self) -> None:
        clock = FakeClock()
        self.put_lease("old-container", 280, clock.now())
        self.assertEqual(self.run_wait(clock), "released")
        waited = sum(clock.sleeps)
        self.assertGreaterEqual(waited, 280)
        self.assertLess(waited, 300)
        self.assertIn("waiting up to", self.logs[0])

    def test_same_host_lease_is_left_to_openclaw(self) -> None:
        clock = FakeClock()
        self.put_lease("new-host", 280, clock.now())
        self.assertEqual(self.run_wait(clock), "clear")
        self.assertEqual(clock.sleeps, [])

    def test_expired_lease_does_not_wait(self) -> None:
        clock = FakeClock()
        self.put_lease("old-container", -5, clock.now())
        self.assertEqual(self.run_wait(clock), "clear")

    def test_live_owner_that_keeps_renewing_is_bounded(self) -> None:
        clock = FakeClock()
        original_sleep = clock.sleep

        def renewing_sleep(seconds: float) -> None:
            original_sleep(seconds)
            self.put_lease("old-container", 300, clock.now())

        clock.sleep = renewing_sleep  # type: ignore[method-assign]
        self.put_lease("old-container", 300, clock.now())
        self.assertEqual(self.run_wait(clock, max_wait=60), "timeout")
        self.assertLessEqual(sum(clock.sleeps), 60)

    def test_entrypoint_waits_before_starting_the_gateway(self) -> None:
        entrypoint = (ROOT / "deploy/railway/entrypoint.sh").read_text(encoding="utf-8")
        wait_at = entrypoint.index("wait-gateway-lease.py")
        self.assertLess(entrypoint.index("sync-public-prompt.py \\"), wait_at)
        self.assertLess(wait_at, entrypoint.index("exec runuser -u joseph -- openclaw gateway"))
        dockerfile = (ROOT / "deploy/railway/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY deploy/railway/wait-gateway-lease.py /usr/local/bin/wait-gateway-lease.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
