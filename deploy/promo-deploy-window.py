#!/usr/bin/env python3
"""Hold a production release until no promo campaign is sending.

A gateway restart stops an in-flight campaign run after the 315 s drain, and
there is deliberately no automatic catch-up, so a release that lands inside a
campaign's window costs that campaign its whole day. The README's cron preflight
asks for a quiet gap before merging, but nothing enforced it: on 2026-11-09 the
merge of #184 at 08:31 Harare deployed at 08:42, two minutes into
fruit-trees-promo's 08:40 run, and fruit-trees posted to no group that day.

This gate runs in the deploy job immediately before the release. A moment is
*busy* when an enabled campaign in deploy/promo-crons.json may still be sending
or starts within --lead-minutes (the build and startup verification would
restart the gateway under it). How long a campaign may still be sending is the
engine's own worst case for its committed target list -- the full start jitter,
the longest gap between every pair of sends and the per-send allowance from
workspace/lib/promo_broadcast.py -- capped at the job's timeout, which kills a
run anyway. A campaign without a readable targets.json is given the whole
timeout. If the
next quiet moment is within --max-wait-minutes the gate sleeps until then;
otherwise it fails, naming the next quiet time, so the release can be re-run
from GitHub Actions ("Re-run failed jobs") once that time has passed.

It reads only committed files -- the manifest and each campaign's targets.json
-- and never contacts the gateway. Console-created campaigns (Firestore-scheduled) and
live drift are not visible here; the manual preflight still covers those.

Exit codes: 0 quiet (possibly after waiting), 1 busy beyond the wait bound,
2 bad manifest or arguments.
"""

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

MANIFEST = Path(__file__).resolve().parent / "promo-crons.json"
REPO = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc
# Mirrors of workspace/lib/promo_broadcast.py's defaults (pinned by a test). The
# release runner has no PYTHONPATH for the engine, and importing it would pull in
# the gateway client.
START_JITTER_SECONDS = 900.0
SEND_GAP_MAX_SECONDS = 60.0
SEND_ALLOWANCE_SECONDS = 12.0


def target_count(promo, repo=REPO):
    """Groups in the campaign's committed targets.json, or None if unreadable."""
    path = repo / "workspace" / "cron" / ("%s-promo" % promo.get("slug", "")) / "targets.json"
    try:
        groups = json.loads(path.read_text(encoding="utf-8")).get("groups")
    except (OSError, ValueError, AttributeError):
        return None
    return len(groups) if isinstance(groups, list) else None


def run_budget_minutes(promo, timeout_seconds, repo=REPO):
    """Worst-case minutes a campaign run may keep sending after its cron fires."""
    timeout = float(promo.get("timeoutSeconds") or timeout_seconds or 7200)
    count = target_count(promo, repo)
    if count is None:
        return timeout / 60.0
    worst = (START_JITTER_SECONDS + count * SEND_ALLOWANCE_SECONDS
             + max(count - 1, 0) * SEND_GAP_MAX_SECONDS)
    return min(worst, timeout) / 60.0


def daily_starts(manifest, repo=REPO):
    """[(name, tz, hour, minute, run_minutes)] for every enabled campaign."""
    starts = []
    for promo in manifest.get("promos", []):
        if promo.get("enabled", True) is False:
            continue
        fields = str(promo.get("cron", "")).split()
        if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
            raise ValueError("%s: only daily 'M H * * *' schedules are understood, got %r"
                             % (promo.get("name"), promo.get("cron")))
        minute, hour = int(fields[0]), int(fields[1])
        if not (0 <= minute < 60 and 0 <= hour < 24):
            raise ValueError("%s: bad cron time %r" % (promo.get("name"), promo.get("cron")))
        starts.append((promo["name"], ZoneInfo(promo.get("tz") or "UTC"), hour, minute,
                       run_budget_minutes(promo, manifest.get("timeoutSeconds"), repo)))
    return starts


def runs_near(starts, now):
    """Concrete UTC start times from yesterday to tomorrow around `now`."""
    out = []
    for name, tz, hour, minute, run_minutes in starts:
        local_day = now.astimezone(tz).date()
        for offset in (-2, -1, 0, 1):
            day = local_day + dt.timedelta(days=offset)
            local = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            out.append((local.astimezone(UTC), name, run_minutes))
    return sorted(out)


def conflicts(starts, at, lead_minutes):
    """Campaign runs that make `at` unsafe for a restart."""
    lead = dt.timedelta(minutes=lead_minutes)
    return [(start, name) for start, name, run_minutes in runs_near(starts, at)
            if at - dt.timedelta(minutes=run_minutes) < start <= at + lead]


def next_quiet(starts, now, lead_minutes):
    """The first whole minute at or after `now` with no conflicting run."""
    at = now.replace(second=0, microsecond=0)
    if at < now:
        at += dt.timedelta(minutes=1)
    if not conflicts(starts, now, lead_minutes):
        return now
    for _ in range(2 * 24 * 60):
        if not conflicts(starts, at, lead_minutes):
            return at
        at += dt.timedelta(minutes=1)
    return None


def describe(moment, tz):
    return "%s (%s UTC)" % (moment.astimezone(tz).strftime("%H:%M %Z"),
                            moment.astimezone(UTC).strftime("%H:%M"))


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--manifest", default=str(MANIFEST))
    parser.add_argument("--repo", default=str(REPO),
                        help="checkout whose workspace/cron/*/targets.json sizes each run")
    parser.add_argument("--lead-minutes", type=int, default=20,
                        help="time from this gate to the gateway restart")
    parser.add_argument("--max-wait-minutes", type=int, default=45)
    parser.add_argument("--now", help="ISO-8601 instant to evaluate instead of the clock")
    parser.add_argument("--check-only", action="store_true",
                        help="report and exit; never sleep")
    args = parser.parse_args(argv)
    for name in ("lead_minutes", "max_wait_minutes"):
        if getattr(args, name) < 0:
            parser.error("--%s must not be negative" % name.replace("_", "-"))
    return args


def main(argv=None, clock=None, sleep=time.sleep):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        starts = daily_starts(manifest, Path(args.repo))
    except (OSError, ValueError, KeyError) as exc:
        print("::error::promo deploy window: %s" % exc, file=sys.stderr)
        return 2
    if clock is None:
        clock = lambda: dt.datetime.now(UTC)
    if args.now:
        now = dt.datetime.fromisoformat(args.now)
        if now.tzinfo is None:
            print("::error::--now needs a timezone", file=sys.stderr)
            return 2
        clock = lambda: now.astimezone(UTC)
    shown_tz = starts[0][1] if starts else UTC

    now = clock()
    busy = conflicts(starts, now, args.lead_minutes)
    if not busy:
        print("Promo window quiet at %s; releasing." % describe(now, shown_tz))
        return 0
    quiet = next_quiet(starts, now, args.lead_minutes)
    names = ", ".join("%s at %s" % (name, describe(start, shown_tz)) for start, name in busy)
    print("Promo campaigns in the restart window: %s." % names)
    if quiet is None:
        print("::error::No quiet window in the next 48 hours; check deploy/promo-crons.json.",
              file=sys.stderr)
        return 1
    wait = (quiet - now).total_seconds()
    if args.check_only or wait > args.max_wait_minutes * 60:
        print("::error::A release now would restart the gateway during a campaign run. "
              "Next quiet window: %s. Re-run this job after then, or dispatch the "
              "workflow with ignore_promo_window if the restart is deliberate."
              % describe(quiet, shown_tz), file=sys.stderr)
        return 1
    print("Waiting %d min for the quiet window at %s." % (round(wait / 60), describe(quiet, shown_tz)))
    sys.stdout.flush()
    sleep(wait)
    return 0


if __name__ == "__main__":
    sys.exit(main())
