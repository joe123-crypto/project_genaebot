#!/usr/bin/env python3

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import dashboard_telemetry


def fake_endpoint(path: str) -> str:
    return f"http://localhost:3000{path}"


def frozen_datetime(fixed: datetime) -> type[datetime]:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed

    return FrozenDatetime


class FakeResponse:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PublishTaskStatusTests(unittest.TestCase):
    def publish(self, **overrides) -> tuple[bool, mock.Mock]:
        params = {
            "user_id": "user-1",
            "task_id": "search_apply_jobs",
            "service": "job_scout",
            "status": "active",
            "schedule_label": "Daily - 9:00 AM",
            "timezone_name": "Africa/Harare",
            "next_run_at": "2026-07-17T07:00:00Z",
            "last_run_at": "2026-07-16T07:00:00Z",
            "last_run_status": "success",
            "last_run_summary": "2 application(s) sent",
        }
        params.update(overrides)
        urlopen = mock.Mock(return_value=FakeResponse({"ok": True}))
        with (
            mock.patch.object(dashboard_telemetry, "api_key", return_value="secret"),
            mock.patch.object(dashboard_telemetry, "endpoint", side_effect=fake_endpoint),
            mock.patch.object(dashboard_telemetry.urllib.request, "urlopen", urlopen),
        ):
            ok = dashboard_telemetry.publish_task_status(**params)
        return ok, urlopen

    def test_posts_the_dashboard_task_contract(self):
        ok, urlopen = self.publish()
        self.assertTrue(ok)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:3000/internal/dashboard/tasks")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload, {
            "userId": "user-1",
            "taskId": "search_apply_jobs",
            "service": "job_scout",
            "enabled": True,
            "status": "active",
            "scheduleLabel": "Daily - 9:00 AM",
            "timezone": "Africa/Harare",
            "nextRunAt": "2026-07-17T07:00:00Z",
            "lastRunAt": "2026-07-16T07:00:00Z",
            "lastRunStatus": "success",
            "lastRunSummary": "2 application(s) sent",
        })

    def test_returns_false_without_raising_on_http_error(self):
        error = urllib.error.HTTPError("url", 400, "Bad Request", None, io.BytesIO(b"{}"))
        with (
            mock.patch.object(dashboard_telemetry, "api_key", return_value="secret"),
            mock.patch.object(dashboard_telemetry.urllib.request, "urlopen", side_effect=error),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            ok = dashboard_telemetry.publish_task_status(
                user_id="user-1", task_id="search_apply_jobs", service="job_scout", status="paused",
            )
        self.assertFalse(ok)
        self.assertIn("dashboard telemetry skipped", stderr.getvalue())

    def test_returns_false_without_raising_on_network_error(self):
        with (
            mock.patch.object(dashboard_telemetry, "api_key", return_value="secret"),
            mock.patch.object(
                dashboard_telemetry.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("unreachable"),
            ),
            redirect_stderr(io.StringIO()),
        ):
            ok = dashboard_telemetry.publish_task_status(
                user_id="user-1", task_id="deliver_results", service="delivery", status="active",
            )
        self.assertFalse(ok)

    def test_returns_false_when_api_key_missing(self):
        urlopen = mock.Mock()
        with (
            mock.patch.object(dashboard_telemetry, "api_key", return_value=""),
            mock.patch.object(dashboard_telemetry.urllib.request, "urlopen", urlopen),
            redirect_stderr(io.StringIO()),
        ):
            ok = dashboard_telemetry.publish_task_status(
                user_id="user-1", task_id="search_apply_jobs", service="job_scout", status="paused",
            )
        self.assertFalse(ok)
        urlopen.assert_not_called()

    def test_returns_false_when_server_reports_not_ok(self):
        with (
            mock.patch.object(dashboard_telemetry, "api_key", return_value="secret"),
            mock.patch.object(
                dashboard_telemetry.urllib.request,
                "urlopen",
                return_value=FakeResponse({"ok": False, "error": "taskId is invalid."}),
            ),
            redirect_stderr(io.StringIO()),
        ):
            ok = dashboard_telemetry.publish_task_status(
                user_id="user-1", task_id="search_apply_jobs", service="job_scout", status="paused",
            )
        self.assertFalse(ok)


class NextDailyRunTests(unittest.TestCase):
    def test_before_the_daily_time_uses_today(self):
        fixed = datetime(2026, 7, 16, 8, 0, tzinfo=ZoneInfo("Africa/Harare"))
        with mock.patch.object(dashboard_telemetry, "datetime", frozen_datetime(fixed)):
            result = dashboard_telemetry.next_daily_run_iso(9, 0, "Africa/Harare")
        self.assertEqual(result, "2026-07-16T07:00:00Z")

    def test_after_the_daily_time_uses_tomorrow(self):
        fixed = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Africa/Harare"))
        with mock.patch.object(dashboard_telemetry, "datetime", frozen_datetime(fixed)):
            result = dashboard_telemetry.next_daily_run_iso(9, 0, "Africa/Harare")
        self.assertEqual(result, "2026-07-17T07:00:00Z")


class CliTests(unittest.TestCase):
    def test_cli_exits_zero_even_when_the_post_fails(self):
        stdout = io.StringIO()
        with (
            mock.patch.object(dashboard_telemetry, "api_key", return_value="secret"),
            mock.patch.object(
                dashboard_telemetry.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("unreachable"),
            ),
            redirect_stderr(io.StringIO()),
            redirect_stdout(stdout),
        ):
            code = dashboard_telemetry.main([
                "publish",
                "--user-id", "user-1",
                "--task-id", "search_apply_jobs",
                "--service", "job_scout",
                "--status", "paused",
                "--schedule-label", "Automatic runs paused",
                "--last-run-status", "failed",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": False, "taskId": "search_apply_jobs"})

    def test_cli_defaults_last_run_at_to_now(self):
        urlopen = mock.Mock(return_value=FakeResponse({"ok": True}))
        with (
            mock.patch.object(dashboard_telemetry, "api_key", return_value="secret"),
            mock.patch.object(dashboard_telemetry.urllib.request, "urlopen", urlopen),
            redirect_stdout(io.StringIO()),
        ):
            code = dashboard_telemetry.main([
                "publish",
                "--user-id", "user-1",
                "--task-id", "deliver_results",
                "--service", "delivery",
                "--status", "active",
                "--last-run-status", "success",
                "--last-run-summary", "Results delivered",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertTrue(payload["lastRunAt"].endswith("Z"))
        self.assertIsNone(payload["nextRunAt"])


if __name__ == "__main__":
    unittest.main()
