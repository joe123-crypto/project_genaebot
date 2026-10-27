#!/usr/bin/env python3
"""Read Railway billing and metrics without changing the running service.

Capture baseline, plugin-only and allocator-trial reports with shell redirection
outside the repository. Use --since 7d after a full week to assess the $10 target.
"""

import argparse
from datetime import datetime, timezone
import json
import subprocess


def railway(*args):
    result = subprocess.run(["railway", *args, "--json"], capture_output=True,
                            text=True, check=True, timeout=90)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="7d", help="metric window, e.g. 1h or 7d")
    args = parser.parse_args()
    usage = railway("usage")
    metrics = railway("metrics", "--since", args.since, "--cpu", "--memory")
    limits = railway("usage", "limit", "status")
    print(json.dumps({
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "targetMonthlyDollars": 10,
        "targetAverageMemoryMB": 819.2,
        "usage": usage,
        "metrics": metrics,
        "limits": limits,
        "note": "Billing forecast is Railway's estimate, not a spending limit. "
                "Measure a full campaign week and billing deltas before declaring savings.",
    }, indent=2))


if __name__ == "__main__":
    main()
