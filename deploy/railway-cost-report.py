#!/usr/bin/env python3
"""Read Railway billing and metrics without changing the running service.

Capture stable baseline and candidate reports manually outside the repository.
Compare matched full-week workloads; forecasts and linear projections are not invoices.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

PROJECT = "a251f752-3b49-4f72-8a71-4629bf2fced0"
SERVICE = "503baf60-c897-4a77-812a-719195705031"
ENVIRONMENT = "2cae37a4-8e24-41db-87ed-9a3a7c9eff22"
WORKSPACE = "8590bff7-7562-4bf0-9a4b-b83076743523"


def railway(*args):
    result = subprocess.run(["railway", *args, "--json"], capture_output=True,
                            text=True, check=True, timeout=90)
    return json.loads(result.stdout)


def projection(usage, captured):
    """Extrapolate resource usage only; provider forecasts/invoices stay separate."""
    period = usage["billingPeriod"]
    start, end = (datetime.fromisoformat(period[key]) for key in ("start", "end"))
    if not start < captured <= end or end <= start:
        return {"linearResourceCycleDollars": None, "reason": "capture is outside an open billing cycle"}
    elapsed = (captured - start).total_seconds()
    return {"linearResourceCycleDollars": usage["currentUsageDollars"] * (end - start).total_seconds() / elapsed,
            "elapsedDays": elapsed / 86400,
            "assumption": "Period-to-date resource spend continues at its average rate; billing may lag. "
                          "This excludes subscription floors, taxes, credits and other invoice adjustments."}


def compare(before, after):
    if before["scope"] != after["scope"] or before["usage"]["billingPeriod"] != after["usage"]["billingPeriod"]:
        return {"comparable": False, "reason": "scope or billing cycle differs; use Railway history for separate periods"}
    seconds = (datetime.fromisoformat(after["capturedAt"]) - datetime.fromisoformat(before["capturedAt"])).total_seconds()
    delta = after["usage"]["currentUsageDollars"] - before["usage"]["currentUsageDollars"]
    if seconds <= 0 or delta < 0:
        return {"comparable": False, "reason": "timestamps or billing adjustments prevent a valid spend delta"}
    old = {item["label"]: item["currentUsageDollars"] for item in before["usage"]["lineItems"]}
    new = {item["label"]: item["currentUsageDollars"] for item in after["usage"]["lineItems"]}
    return {"comparable": True, "elapsedDays": seconds / 86400, "resourceSpendDeltaDollars": delta,
            "resourceDollarsPerDay": delta * 86400 / seconds,
            "byResourceDeltaDollars": {key: new.get(key, 0) - old.get(key, 0) for key in new.keys() | old.keys()},
            "note": "A spend interval is not proof of savings; compare matched full-week workloads."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="7d", help="metric window, e.g. 1h or 7d")
    parser.add_argument("--until", help="freeze the end of the metric window in UTC")
    parser.add_argument("--baseline", type=Path, help="manually captured report at the start of this spend interval")
    args = parser.parse_args()
    captured = datetime.now(timezone.utc)
    usage = railway("usage", "--workspace", WORKSPACE)
    scope = {"workspace": WORKSPACE, "project": PROJECT, "service": SERVICE, "environment": ENVIRONMENT}
    service = ["--project", PROJECT, "--service", SERVICE, "--environment", ENVIRONMENT]
    window = ["--since", args.since, "--until", args.until or captured.isoformat()]
    metrics = railway("metrics", *service, *window, "--cpu", "--memory", "--network", "--volume")
    limits = railway("usage", "limit", "status", "--workspace", WORKSPACE)
    project = railway("usage", "projects", "--workspace", WORKSPACE, "--project", PROJECT)
    deployments = railway("deployment", "list", *service, "--limit", "100")
    variables = railway("variable", "list", *service)
    report = {
        "capturedAt": captured.isoformat(), "scope": scope,
        "targetMonthlyDollars": 10,
        "usage": usage, "projectUsage": project, "metrics": metrics, "limits": limits,
        "resourceProjection": projection(usage, captured),
        "currentRuntimeSettings": {key: variables.get(key) for key in [
            "SOURCE_COMMIT", "GENAIE_CAMPAIGN_TRANSPORT", "GENAIE_CATALOG_IDLE_RETIREMENT"]},
        "deployments": [{"id": item["id"], "status": item["status"], "createdAt": item["createdAt"],
                         "imageDigest": item.get("meta", {}).get("imageDigest"),
                         "commit": item.get("meta", {}).get("commitHash"),
                         "release": item.get("meta", {}).get("cliMessage")}
                        for item in deployments],
        "note": "Use Railway history and invoice line items as the source of truth. Provider forecasts, "
                "linear resource projections and actual invoices are distinct. Current settings do not "
                "describe past deployments. No scheduled collection is installed.",
    }
    if args.baseline:
        report["spendInterval"] = compare(json.loads(args.baseline.read_text()), report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
