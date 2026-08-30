"""Best-effort publisher for the external Agent Genaie task dashboard."""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .agent_genaie import api_key, endpoint


REQUEST_TIMEOUT_SECONDS = 10


def next_daily_run_iso(hour: int, minute: int, tz_name: str) -> str:
  now = datetime.now(ZoneInfo(tz_name))
  candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
  if candidate <= now:
    candidate += timedelta(days=1)
  return candidate.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def publish_task_status(
  *,
  user_id: str,
  task_id: str,
  service: str,
  status: str,
  schedule_label: str | None = None,
  timezone_name: str | None = None,
  next_run_at: str | None = None,
  last_run_at: str | None = None,
  last_run_status: str | None = None,
  last_run_summary: str | None = None,
  enabled: bool = True,
) -> bool:
  """POST one task snapshot, returning False and warning on every failure."""
  try:
    key = api_key()
    if not key:
      raise RuntimeError("AGENT_GENAI_INTERNAL_API_KEY is not configured")
    payload = {
      "userId": user_id,
      "taskId": task_id,
      "service": service,
      "enabled": enabled,
      "status": status,
      "scheduleLabel": schedule_label,
      "timezone": timezone_name,
      "nextRunAt": next_run_at,
      "lastRunAt": last_run_at,
      "lastRunStatus": last_run_status,
      "lastRunSummary": last_run_summary,
    }
    request = urllib.request.Request(
      endpoint("/internal/dashboard/tasks"),
      data=json.dumps(payload).encode("utf-8"),
      method="POST",
      headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
      },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
      body = response.read().decode("utf-8")
    result = json.loads(body) if body else {}
    if result.get("ok") is not True:
      raise RuntimeError(f"unexpected response: {body[:200]}")
    return True
  except Exception as err:  # noqa: BLE001 - telemetry must never break a run
    print(f"dashboard telemetry skipped ({task_id}): {err}", file=sys.stderr)
    return False
