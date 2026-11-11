#!/usr/bin/env python3
"""Hold a production release until no promo campaign is sending.

A gateway restart stops an in-flight campaign run after the 315 s drain, and
there is deliberately no automatic catch-up, so a release that lands inside a
campaign's window costs that campaign its whole day. The README's cron preflight
asks for a quiet gap before merging, but nothing enforced it: on 2026-11-09 the
merge of #184 at 08:31 Harare deployed at 08:42, two minutes into
fruit-trees-promo's 08:40 run, and fruit-trees posted to no group that day.

This gate runs in the deploy job immediately before the release. A moment is
*busy* when an enabled campaign may still be sending or starts within
--lead-minutes (the build and startup verification would restart the gateway
under it). How long a campaign may still be sending is the engine's own worst
case for its target list -- the full start jitter, the longest gap between every
pair of sends and the per-send allowance from workspace/lib/promo_broadcast.py
-- capped at the job's timeout, which kills a run anyway. If the next quiet
moment is within --max-wait-minutes the gate sleeps until then; otherwise it
fails, naming the next quiet time, so the release can be re-run from GitHub
Actions ("Re-run failed jobs") once that time has passed.

The campaign list comes from the gateway (deploy/gateway_campaigns.py), because
the schedules and target counts are operator data that now lives only in
Firestore. That fixed the blind spot this gate shipped with: campaigns created
in the console were invisible to it, so it never held a release out of their
sending windows, and a live schedule override was invisible too. If the gateway
cannot be reached the gate refuses rather than guesses -- a gate that cannot see
must not wave a release through -- and the workflow's ignore_promo_window input
is the deliberate override.

Every campaign is treated as if it could fire at its time on any day. A campaign
that skips some weekdays then holds a release slightly more often than it had
to, which is the right direction for this to err in.

It also reports the two schedule guardrails that used to be repository tests
over the committed group lists and cron manifest (see
``gateway_campaigns.problems``): a campaign that cannot finish inside its own
job timeout fails this gate, and two campaigns overlapping on one WhatsApp line
are a warning.

Exit codes: 0 quiet (possibly after waiting), 1 busy beyond the wait bound, a
campaign that cannot finish its own run, or the gateway unreachable; 2 bad input.
"""

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateway_campaigns  # noqa: E402

UTC = dt.timezone.utc


def run_budget_minutes(row):
    """Worst-case minutes a campaign run may keep sending after its cron fires."""
    return gateway_campaigns.run_budget_seconds(row) / 60.0


def daily_starts(rows):
    """[(name, tz, hour, minute, run_minutes)] for every enabled campaign."""
    starts = []
    for row in gateway_campaigns.enabled(rows):
        hour, minute = int(row["hour"]), int(row["minute"])
        if not (0 <= minute < 60 and 0 <= hour < 24):
            raise ValueError("%s: bad schedule time %02d:%02d" % (row["slug"], hour, minute))
        try:
            zone = ZoneInfo(row["timezone"])
        except Exception as ex:
            raise ValueError("%s: unusable timezone %r (%s)" % (row["slug"], row["timezone"], ex))
        starts.append((row.get("jobName") or row["slug"], zone, hour, minute,
                       run_budget_minutes(row)))
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
    parser.add_argument("--campaigns", metavar="FILE",
                        help="a saved `console-read campaigns` reply to use instead "
                             "of asking the gateway, for reproducing a decision")
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


def main(argv=None, clock=None, sleep=time.sleep, read=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.campaigns:
            rows = gateway_campaigns.parse(Path(args.campaigns).read_text(encoding="utf-8"))
        else:
            rows = (read or gateway_campaigns.read)()
        starts = daily_starts(rows)
    except gateway_campaigns.Unavailable as exc:
        # Refuse, do not guess: holding a release that did not need holding costs
        # a re-run, while releasing into a campaign's window costs that campaign
        # its whole day, which is the incident this gate exists because of.
        print("::error::Cannot read the live campaigns, so this gate cannot tell "
              "whether a campaign is sending: %s. Fix the gateway access and "
              "re-run this job, or dispatch the workflow with "
              "ignore_promo_window if the restart is deliberate." % exc, file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as exc:
        print("::error::promo deploy window: %s" % exc, file=sys.stderr)
        return 2
    print("Read %d campaign(s) from the gateway, %d enabled."
          % (len(rows), len(gateway_campaigns.enabled(rows))))
    # The schedule guardrails. These used to be repository tests over the
    # committed group lists and cron manifest; the data is in Firestore now, so
    # this is where they are checked, against what the bot is actually doing.
    found = gateway_campaigns.problems(rows)
    for severity, message in found:
        print("::%s::%s" % (severity, message), file=sys.stderr)
    if any(severity == "error" for severity, _ in found):
        print("::error::A campaign cannot finish inside its own job timeout, so it "
              "loses groups every day. Fix it in the console (or raise the job "
              "timeout) and re-run this job; dispatch with ignore_promo_window if "
              "the release must go out first.", file=sys.stderr)
        return 1
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
        print("::error::No quiet window in the next 48 hours; check the campaign "
              "schedules in the console.", file=sys.stderr)
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
