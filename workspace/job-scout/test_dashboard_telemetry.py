from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from unittest import mock
import unittest
import urllib.error

from jobscout.integrations import dashboard_telemetry


class DashboardTelemetryTests(unittest.TestCase):
  def test_publish_posts_exact_contract_with_ten_second_timeout(self) -> None:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok":true}'
    with mock.patch.object(dashboard_telemetry, "api_key", return_value="secret"), \
         mock.patch.object(dashboard_telemetry, "endpoint", side_effect=lambda path: f"https://agent.test{path}"), \
         mock.patch.object(dashboard_telemetry.urllib.request, "urlopen", return_value=response) as urlopen:
      result = dashboard_telemetry.publish_task_status(
        user_id="user-1",
        task_id="search_apply_jobs",
        service="job_scout",
        status="active",
        schedule_label="Daily - 8:00 AM",
        timezone_name="Africa/Harare",
        next_run_at="2026-08-02T06:00:00Z",
        last_run_at="2026-08-01T06:00:00Z",
        last_run_status="success",
        last_run_summary="2 application(s) sent",
      )
    self.assertTrue(result)
    request = urlopen.call_args.args[0]
    self.assertEqual(request.full_url, "https://agent.test/internal/dashboard/tasks")
    self.assertEqual(request.method, "POST")
    self.assertEqual(request.get_header("Authorization"), "Bearer secret")
    self.assertEqual(urlopen.call_args.kwargs["timeout"], 10)
    self.assertEqual(json.loads(request.data)["userId"], "user-1")
    self.assertEqual(json.loads(request.data)["taskId"], "search_apply_jobs")

  def test_missing_key_warns_and_returns_false(self) -> None:
    stderr = io.StringIO()
    with mock.patch.object(dashboard_telemetry, "api_key", return_value=""), mock.patch("sys.stderr", stderr):
      result = dashboard_telemetry.publish_task_status(
        user_id="user-1",
        task_id="deliver_results",
        service="delivery",
        status="active",
      )
    self.assertFalse(result)
    self.assertIn("dashboard telemetry skipped (deliver_results)", stderr.getvalue())
    self.assertIn("AGENT_GENAI_INTERNAL_API_KEY is not configured", stderr.getvalue())

  def test_network_and_response_failures_never_raise(self) -> None:
    for side_effect, body in (
      (urllib.error.URLError("offline"), None),
      (None, b'{"ok":false}'),
    ):
      response = mock.MagicMock()
      response.__enter__.return_value.read.return_value = body
      stderr = io.StringIO()
      with self.subTest(side_effect=side_effect), \
           mock.patch.object(dashboard_telemetry, "api_key", return_value="secret"), \
           mock.patch.object(dashboard_telemetry, "endpoint", return_value="https://agent.test/internal/dashboard/tasks"), \
           mock.patch.object(dashboard_telemetry.urllib.request, "urlopen", side_effect=side_effect, return_value=response), \
           mock.patch("sys.stderr", stderr):
        self.assertFalse(dashboard_telemetry.publish_task_status(
          user_id="user-1",
          task_id="search_apply_jobs",
          service="job_scout",
          status="active",
        ))
        self.assertIn("dashboard telemetry skipped (search_apply_jobs)", stderr.getvalue())

  def test_next_daily_run_is_future_utc_harare_eight_am(self) -> None:
    value = dashboard_telemetry.next_daily_run_iso(8, 0, "Africa/Harare")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    self.assertGreater(parsed, datetime.now(timezone.utc))
    self.assertEqual((parsed.hour, parsed.minute), (6, 0))


if __name__ == "__main__":
  unittest.main()
