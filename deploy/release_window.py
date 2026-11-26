"""When a bot can be restarted without cutting a campaign off.

A restart stops an in-flight campaign run after the drain, and there is
deliberately no catch-up, so a restart inside a campaign's sending window costs
that campaign its whole day (see ``deploy/promo-deploy-window.py`` for the
incident). This is the arithmetic that decides it, in one place, for both of
the things that restart a bot:

* the release gate in CI (``promo-deploy-window.py``), which holds a gateway
  release until it is quiet; and
* every bot's own console API (``GET /internal/console/release-window``), which
  the operator console asks before it rolls a new image out to a workspace's
  bot. A workspace's bot is the only thing that can see its own campaigns -- CI's
  read-scoped gate token is never given to one.

A moment is *busy* when an enabled campaign may still be sending, or starts
within ``lead_minutes`` (the restart itself takes that long to land). How long a
campaign may still be sending is ``gateway_campaigns.run_budget_seconds``: the
engine's own worst case for its target list, capped at the job timeout.

Every campaign is treated as if it could fire at its times on any day, which
errs towards holding a restart rather than landing one in a run.

Std-lib only: it runs on a CI runner and inside the bot image.
"""

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateway_campaigns  # noqa: E402

UTC = dt.timezone.utc
# From the decision to the restart: the CI gate's build and startup check, or
# an image pull and boot for a workspace's bot.
LEAD_MINUTES = 20


def run_budget_minutes(row):
    """Worst-case minutes a campaign run may keep sending after its cron fires."""
    return gateway_campaigns.run_budget_seconds(row) / 60.0


def daily_starts(rows):
    """[(name, tz, hour, minute, run_minutes)]: every time an enabled campaign fires."""
    starts = []
    for row in gateway_campaigns.enabled(rows):
        hour, minute = int(row["hour"]), int(row["minute"])
        if not (0 <= minute < 60 and 0 <= hour < 24):
            raise ValueError("%s: bad schedule time %02d:%02d" % (row["slug"], hour, minute))
        try:
            ZoneInfo(row["timezone"])
        except Exception as ex:
            raise ValueError("%s: unusable timezone %r (%s)" % (row["slug"], row["timezone"], ex))
        run_minutes = run_budget_minutes(row)
        for name, zone, hour, minute in gateway_campaigns.starts(row):
            starts.append((name, zone, hour, minute, run_minutes))
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


def _stamp(moment):
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z") if moment else None


def window(rows, now, lead_minutes=LEAD_MINUTES):
    """The answer a bot gives when asked whether it may be restarted now.

    ``quiet`` is the whole decision. ``nextQuietAt`` is when it next would be
    (``now`` itself when quiet, null when nothing is quiet for 48 hours), and
    ``busy`` names the runs in the way, for whoever is reading the log."""
    starts = daily_starts(rows)
    busy = conflicts(starts, now, lead_minutes)
    return {
        "quiet": not busy,
        "at": _stamp(now),
        "nextQuietAt": _stamp(now if not busy else next_quiet(starts, now, lead_minutes)),
        "leadMinutes": lead_minutes,
        "busy": [{"name": name, "start": _stamp(start)} for start, name in busy],
    }
