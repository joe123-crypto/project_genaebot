#!/usr/bin/env python3
"""Best-effort dashboard task telemetry publisher for Agent Genaie.

Publishes one live task status snapshot per (user, task) to
POST /internal/dashboard/tasks so the signed-in dashboard can render the
agent's schedule and last-run outcomes. Telemetry must never break a run:
every failure path logs one warning to stderr and reports False.

Importable from Python (job-scout dispatch) and runnable as a CLI from bash
cron scripts (webetu meals). Both callers resolve the server and API key via
genaie_env, so no new configuration is required.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from genaie_env import api_key, endpoint


TASK_SERVICES = {
    "reserve_meals": "webetu",
    "search_apply_jobs": "job_scout",
    "deliver_results": "delivery",
}
STATUSES = ("active", "running", "paused", "failed", "disabled")
LAST_RUN_STATUSES = ("success", "partial", "failed", "skipped", "action_required")

REQUEST_TIMEOUT_SECONDS = 10


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def next_daily_run_iso(hour: int, minute: int, tz_name: str) -> str:
    """UTC ISO timestamp of the next wall-clock hour:minute in tz_name."""
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
    """POST one task snapshot. Returns False (never raises) on any failure."""
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


def cmd_publish(args: argparse.Namespace) -> int:
    ok = publish_task_status(
        user_id=args.user_id,
        task_id=args.task_id,
        service=args.service,
        status=args.status,
        schedule_label=args.schedule_label,
        timezone_name=args.timezone,
        next_run_at=args.next_run_at,
        last_run_at=args.last_run_at or utc_now_iso(),
        last_run_status=args.last_run_status,
        last_run_summary=args.last_run_summary,
        enabled=not args.disabled,
    )
    print(json.dumps({"ok": ok, "taskId": args.task_id}))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)

    publish = subparsers.add_parser("publish")
    publish.add_argument("--user-id", required=True)
    publish.add_argument("--task-id", required=True, choices=sorted(TASK_SERVICES))
    publish.add_argument("--service", required=True, choices=sorted(set(TASK_SERVICES.values())))
    publish.add_argument("--status", required=True, choices=STATUSES)
    publish.add_argument("--schedule-label")
    publish.add_argument("--timezone")
    publish.add_argument("--next-run-at")
    publish.add_argument("--last-run-at")
    publish.add_argument("--last-run-status", choices=LAST_RUN_STATUSES)
    publish.add_argument("--last-run-summary")
    publish.add_argument("--disabled", action="store_true")
    publish.set_defaults(func=cmd_publish)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
