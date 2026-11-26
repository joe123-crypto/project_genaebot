"""The live campaign list, for the two release gates that used to read files.

``deploy/promo-deploy-window.py`` holds a release until no campaign is sending,
and ``deploy/railway-release.py`` checks that the gateway came back up with every
enabled campaign's cron job. Both used to work entirely from the checkout: the
schedule and timeout from ``deploy/promo-crons.json``, the group count from each
campaign's ``targets.json``, the job name from the manifest entry.

That is operator data and it now lives only in Firestore. So both gates ask the
gateway for ``console-read campaigns`` -- the same read the console uses, which
reports every campaign whatever kind it is.

There are two ways in, because the two callers are in different places. An
operator runs ``deploy/gateway-campaigns.sh``, which goes in over ``railway
ssh``. CI cannot: ``railway ssh`` needs an SSH key registered with the Railway
account and a runner has none, while ``RAILWAY_TOKEN`` authenticates only the
HTTP API -- which is how the first release after this migration failed its own
gate with the gateway perfectly healthy. So CI fetches the same read over HTTPS
from the console API, holding ``CONSOLE_API_GATE_TOKEN``: a second bearer token
the API accepts on this one route and refuses everywhere else, so a gate that
only reads cannot carry the authority to write.

Two things improve by asking rather than reading files. The gate now sees
campaigns created in the console, which were invisible to it before and whose
sending windows it therefore never held a deploy for. And it sees the schedule
actually in force, rather than the shipped default a live override may have
replaced.

The cost is a dependency on the gateway being reachable from CI, so a failure
here is deliberately not softened: the caller is a gate, and a gate that cannot
see is not allowed to wave a release through. :class:`Unavailable` carries what
went wrong; ``promo-deploy-window.py`` turns it into a refusal the operator can
override with the workflow's ``ignore_promo_window`` input.

Std-lib only, like everything else the deploy job runs.
"""

import datetime as dt
import json
import math
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).resolve().parent / "gateway-campaigns.sh"
# How to address the live gateway, in one place: deploy/railway-release.py takes
# its own scope from here. `railway ssh` otherwise resolves the project from a
# linked directory, and a CI checkout is never linked -- the first release after
# the campaign data moved to Firestore failed this gate on nothing but that --
# so the scope goes in the wrapper's environment, which is also how
# deploy/railway/README.md tells an operator to run from an unlinked checkout.
PROJECT = "a251f752-3b49-4f72-8a71-4629bf2fced0"
SERVICE = "503baf60-c897-4a77-812a-719195705031"
ENVIRONMENT = "2cae37a4-8e24-41db-87ed-9a3a7c9eff22"
SCOPE = ["--project", PROJECT, "--service", SERVICE, "--environment", ENVIRONMENT]
RAILWAY_SCOPE = {"RAILWAY_PROJECT": PROJECT, "RAILWAY_SERVICE": SERVICE,
                 "RAILWAY_ENVIRONMENT": ENVIRONMENT}
# The console API, reached over the Railway service domain that targets its port
# (see deploy/railway/README.md). GENAIE_CONSOLE_URL overrides it for a staging
# gateway; the token's presence is what decides which way in is used.
CONSOLE_URL = "https://gateway-production-feba.up.railway.app"
CONSOLE_URL_VAR = "GENAIE_CONSOLE_URL"
GATE_TOKEN_VAR = "CONSOLE_API_GATE_TOKEN"
READ_PATH = "/internal/console/campaigns"
# A campaign list is tens of kilobytes; a megabyte means something else answered.
MAX_REPLY_BYTES = 1 << 20
# Generous: this is one ssh round trip into a container that may be cold, and
# the alternative to waiting is failing a release that was fine.
TIMEOUT_SECONDS = 180
# What a gate is allowed to act on. A campaign is only included when all of
# these are present and of the right type, because a gate that silently defaults
# a missing schedule to midnight is worse than one that says it cannot tell.
REQUIRED = ("slug", "enabled", "hour", "minute", "timezone", "targetCount",
            "timeoutSeconds")


# Mirrors of workspace/lib/promo_broadcast.py's defaults, pinned by a test. The
# release runner has no PYTHONPATH for the engine, and importing it would pull
# in the gateway client.
START_JITTER_SECONDS = 900.0
SEND_GAP_MAX_SECONDS = 60.0
SEND_ALLOWANCE_SECONDS = 12.0
RUN_BUDGET_FRACTION = 0.9
RUN_TIMEOUT_FLOOR_SECONDS = 7200
RUN_TIMEOUT_STEP_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 7200.0
DEFAULT_ACCOUNT = "default"
# The clock schedule entries are reported on (console_schedule.DISPLAY_ZONE):
# an entry row's ``minutes`` are minutes past midnight here, whatever zone its
# own cron is in.
DISPLAY_ZONE = "Africa/Harare"
# The reply recorder only counts a ref code whose prefix it knows, and that list
# is code, in this repository, while the prefixes themselves are campaign data in
# Firestore. So the two can only be compared here, at gate time.
AD_REFS = (Path(__file__).resolve().parent.parent / "workspace" / "plugins"
           / "public-service-gate" / "lib" / "ad-refs.js")


class Unavailable(Exception):
    """The live campaign list could not be read. Never means "no campaigns"."""


def scoped(env=None):
    """The wrapper's environment, with the gateway's address filled in.

    A scope the caller set is left alone, so a run against another project or a
    local container still works."""
    environment = dict(os.environ if env is None else env)
    for name, value in RAILWAY_SCOPE.items():
        if not environment.get(name):
            environment[name] = value
    return environment


def _run(script=None, timeout=TIMEOUT_SECONDS, env=None, runner=None):
    runner = runner or subprocess.run
    try:
        done = runner([str(script or SCRIPT)], capture_output=True, text=True,
                      timeout=timeout, env=scoped(env))
    except FileNotFoundError:
        raise Unavailable("%s is missing" % (script or SCRIPT))
    except subprocess.TimeoutExpired:
        raise Unavailable("the gateway did not answer within %d seconds" % timeout)
    if done.returncode != 0:
        # The last few lines, not the last one: the Railway CLI's own failures
        # end in advice ("Then run this command again."), and the line that says
        # what went wrong is above it.
        detail = [line for line in (done.stderr or done.stdout or "").splitlines()
                  if line.strip()][-3:]
        raise Unavailable("asking the gateway failed (exit %d): %s"
                          % (done.returncode, " / ".join(detail) if detail else "no output"))
    return done.stdout


def parse(text, allow_empty=False):
    """The campaign rows in one ``console-read campaigns`` reply.

    ``console-read`` reports a read it could not complete as ``{"error": ...}``
    with a zero exit, so an error field is checked for before the rows.

    ``allow_empty`` is for a bot asking about itself: a workspace's new bot has
    no campaigns yet, which is quiet, not a sign of reading the wrong bot."""
    try:
        payload = json.loads(text)
    except ValueError as ex:
        raise Unavailable("the gateway's answer is not JSON: %s" % ex)
    if not isinstance(payload, dict):
        raise Unavailable("the gateway's answer is not an object")
    if payload.get("error"):
        raise Unavailable("the gateway refused the read: %s" % payload["error"])
    rows = payload.get("campaigns")
    if not isinstance(rows, list):
        raise Unavailable("the gateway's answer has no campaigns list")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise Unavailable("a campaign row is not an object")
        missing = [name for name in REQUIRED if row.get(name) is None]
        if missing:
            raise Unavailable("campaign %r is missing %s"
                              % (row.get("slug"), ", ".join(missing)))
        if row.get("error"):
            raise Unavailable("campaign %r cannot be read: %s" % (row["slug"], row["error"]))
        out.append(row)
    if not out and not allow_empty:
        # Every campaign disabled is normal; none existing at all means this is
        # not the gateway the release is about to replace.
        raise Unavailable("the gateway reports no campaigns at all")
    return out


def console_url(env=None):
    """Where the console API is, without a trailing slash."""
    environment = os.environ if env is None else env
    return (environment.get(CONSOLE_URL_VAR) or CONSOLE_URL).rstrip("/")


def _fetch(url, token, timeout=TIMEOUT_SECONDS, opener=None):
    """The campaigns read over HTTPS, as text.

    Nothing here may carry the token into a message: a gate's failure is read
    from a public build log."""
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as reply:
            return reply.read(MAX_REPLY_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        hint = {401: " (is CONSOLE_API_GATE_TOKEN the one the gateway has?)",
                404: " (is the gateway deployed with the console API's gate route?)"}
        raise Unavailable("the console API answered %d for %s%s"
                          % (exc.code, READ_PATH, hint.get(exc.code, "")))
    except (urllib.error.URLError, OSError) as exc:
        raise Unavailable("cannot reach the console API at %s (%s)"
                          % (url.split(READ_PATH)[0], exc))
    except UnicodeError as exc:
        raise Unavailable("the console API's answer is not text (%s)" % exc)


def read(env=None, **inject):
    """Every campaign the gateway knows, as ``console-read`` reports them.

    Over HTTPS when a gate token is configured, which is how CI reads it, and
    otherwise through the ``railway ssh`` wrapper, which is how an operator
    does. Either way a read that did not complete raises, never returns less."""
    environment = os.environ if env is None else env
    token = (environment.get(GATE_TOKEN_VAR) or "").strip()
    if token:
        return parse(_fetch(console_url(environment) + READ_PATH, token,
                            **{name: inject[name] for name in ("timeout", "opener")
                               if name in inject}))
    return parse(_run(env=environment, **inject))


def enabled(rows):
    return [row for row in rows if row.get("enabled")]


def job_names(rows):
    """The primary jobs required by the release startup check.

    A campaign converted from a script keeps its original ``<slug>-promo`` job;
    one created in the console has ``console-<slug>-promo``. Either way the
    gateway reports the name. Its campaign switch can be on while its primary
    entry is paused, so also honor the last confirmed primary pause."""
    names = set()
    for row in enabled(rows):
        if any(entry.get("entryId") == "primary" and (entry.get("confirmed") or {}).get("paused") is True
               for entry in row.get("scheduleEntries") or []):
            continue
        name = row.get("jobName")
        if not isinstance(name, str) or not name:
            raise Unavailable("campaign %r has no job name" % row.get("slug"))
        names.add(name)
    return names


def worst_case_seconds(row, count=None):
    """How long a run may still be sending after its cron fires.

    The engine's own worst case: the full start jitter, then every gap between
    sends at the top of its range, plus a per-send allowance. ``count`` narrows
    it to one line's share of the target list."""
    sends = int(row["targetCount"] if count is None else count)
    return (START_JITTER_SECONDS + sends * SEND_ALLOWANCE_SECONDS
            + max(sends - 1, 0) * SEND_GAP_MAX_SECONDS)


def run_timeout_seconds(count):
    """The job timeout the bot gives a ``count``-group campaign; a mirror of
    ``promo_broadcast.run_timeout_seconds``, pinned by a test."""
    count = max(int(count), 0)
    sends = count * SEND_ALLOWANCE_SECONDS + max(count - 1, 0) * SEND_GAP_MAX_SECONDS
    needed = START_JITTER_SECONDS + sends / RUN_BUDGET_FRACTION
    steps = math.ceil(needed / RUN_TIMEOUT_STEP_SECONDS - 1e-9)
    return max(RUN_TIMEOUT_FLOOR_SECONDS, int(steps * RUN_TIMEOUT_STEP_SECONDS))


def run_budget_seconds(row):
    """The worst case, capped at the job timeout that kills the run anyway."""
    timeout = float(row.get("timeoutSeconds") or DEFAULT_TIMEOUT_SECONDS)
    return min(worst_case_seconds(row), timeout)


def starts(row):
    """``[(label, zone, hour, minute)]``: every time this campaign's crons fire.

    A campaign with schedule entries fires once per live entry, at the entry's
    time. Its own ``hour``/``minute`` used to be all this read, and they are
    only where the campaign started: moving an entry on the timeline leaves
    them behind, which is how genaie-marketing-software was held for at 03:50
    while its jobs fired at 00:05 and 10:55. An entry whose move has not been
    confirmed yet still has its old job, so the registered time counts too.
    A paused or deleted entry fires nothing. A skip only covers one date, so it
    still counts, in the same spirit as treating every day as a firing day.

    A row without entries (an older bot) falls back to its own time."""
    entries = row.get("scheduleEntries")
    if not isinstance(entries, list) or not entries:
        return [(row.get("jobName") or row["slug"], ZoneInfo(row["timezone"]),
                 int(row["hour"]), int(row["minute"]))]
    zone = ZoneInfo(DISPLAY_ZONE)
    found = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise Unavailable("campaign %r has a schedule entry that is not an object"
                              % row["slug"])
        label = "%s (%s)" % (row["slug"], entry.get("entryId") or "entry")
        times = []
        if not entry.get("deleted") and not entry.get("paused"):
            times.append(entry.get("minutes"))
        confirmed = entry.get("confirmed")
        if (isinstance(confirmed, dict) and not confirmed.get("deleted")
                and not confirmed.get("paused")):
            times.append(confirmed.get("minutes"))
        for minutes in dict.fromkeys(times):
            if minutes is None:
                continue
            if type(minutes) is not int or not 0 <= minutes < 24 * 60:
                raise Unavailable("campaign %r has a schedule entry at %r minutes"
                                  % (row["slug"], minutes))
            found.append((label, zone, minutes // 60, minutes % 60))
    return found


def _per_account(row):
    """``{line: count}`` for one campaign, however the gateway reports it."""
    accounts = row.get("accounts")
    if isinstance(accounts, dict) and accounts:
        return dict((str(line), int(count)) for line, count in accounts.items())
    return {DEFAULT_ACCOUNT: int(row["targetCount"])}


def recorder_prefixes(path=None):
    """The ref-code prefixes the reply recorder counts, or ``None`` if unreadable."""
    try:
        source = (path or AD_REFS).read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r"AD_REF_PREFIXES = \[([^\]]*)\]", source)
    if not found:
        return None
    return set(re.findall(r'"([A-Z]+)"', found.group(1)))


def problems(rows, *, prefixes=None):
    """``[(severity, message)]`` for schedules that cannot do what they promise.

    Guardrails that used to be repository tests, because every input was a
    committed file: the group lists, the schedules and the job timeouts. They are
    operator data now, so the only place they can be checked is against the live
    list -- which is also the only place worth checking, since the committed
    copies had drifted from what the bot was actually doing.

    Every one is a ``warning``. None of them is something a release changes, and
    none of them stops a campaign posting:

    - A campaign whose job timeout is shorter than its groups need at full
      spacing. The bot derives the timeout from the group count and its
      reconciler keeps it there, so this is a timeout that has not caught up
      yet; meanwhile the engine fits the run inside it by tightening the gaps.
      This used to be an ``error`` that failed the release, and on 2026-11-20 it
      held every release back for one campaign that had grown to 103 groups.
    - Two campaigns on the same WhatsApp line where the earlier may still be
      posting when the later starts. The 20-60 s spacing is per line, so two
      overlapping runs double that line's send rate and defeat it.
    - A campaign whose run may still be going when its own next entry starts.
      That entry finds the run lock taken and posts nothing.
    - A tracked campaign whose ref prefix the reply recorder does not count.
    """
    found = []
    live = enabled(rows)
    for row in sorted(live, key=lambda row: row["slug"]):
        timeout = float(row.get("timeoutSeconds") or DEFAULT_TIMEOUT_SECONDS)
        worst = worst_case_seconds(row)
        if worst > timeout:
            found.append(("warning",
                          "%s sends to %d group(s), which needs up to %.0f min at full "
                          "spacing, but its job timeout is still %.0f min: until the "
                          "bot's reconciler raises it to %.0f min, its runs tighten "
                          "their send gaps to fit."
                          % (row["slug"], int(row["targetCount"]), worst / 60.0,
                             timeout / 60.0, run_timeout_seconds(row["targetCount"]) / 60.0)))
    # Overlap is per line and per timezone-independent instant, so compare the
    # start times as seconds from midnight UTC on one arbitrary day, around the
    # clock: a run late in the day can still be posting after midnight.
    day = dt.date(2026, 1, 1)
    by_line = {}
    for row in live:
        try:
            fires = starts(row)
        except Exception as ex:
            if isinstance(ex, Unavailable):
                raise
            raise Unavailable("campaign %r has an unusable schedule (%s)" % (row["slug"], ex))
        for _, zone, hour, minute in fires:
            start = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            at = start.astimezone(dt.timezone.utc)
            when = "%02d:%02d" % (hour, minute)
            for line, count in _per_account(row).items():
                by_line.setdefault(line, []).append(
                    (at.hour * 3600 + at.minute * 60, row["slug"], when, count, row))
    seen = set()
    for line, runs in sorted(by_line.items()):
        if len(runs) < 2:
            continue
        runs.sort(key=lambda run: run[:3])
        following = runs[1:] + [(runs[0][0] + 86400,) + runs[0][1:]]
        for (at, slug, when, count, row), (next_at, next_slug, next_when, _, _) in zip(runs, following):
            latest = at + min(worst_case_seconds(row, count),
                              float(row.get("timeoutSeconds") or DEFAULT_TIMEOUT_SECONDS))
            if latest <= next_at:
                continue
            if slug == next_slug:
                # One campaign's runs collide whatever line they are on; say it
                # once, not once per line.
                if slug in seen:
                    continue
                seen.add(slug)
                found.append(("warning",
                              "%s's %s run can still be posting %.0f min after its own "
                              "%s entry starts, and that entry then posts nothing (the "
                              "run lock is taken): move one of its entries."
                              % (slug, when, (latest - next_at) / 60.0, next_when)))
                continue
            found.append(("warning",
                          "%s's %s run (%d group(s) on the %s line) can still be posting "
                          "%.0f min after %s starts at %s, and the send spacing is per "
                          "line: move one of them."
                          % (slug, when, count, line, (latest - next_at) / 60.0,
                             next_slug, next_when)))
    known = recorder_prefixes() if prefixes is None else prefixes
    if known is not None:
        for row in sorted(live, key=lambda row: row["slug"]):
            tracking = row.get("tracking") or {}
            prefix = tracking.get("prefix")
            # A campaign that advertises someone else's number is meant to be
            # absent: its replies never reach the bot, so nothing could count
            # them. Only an unknown prefix on a bot-line campaign is a problem.
            if not prefix or tracking.get("number") or prefix in known:
                continue
            found.append(("warning",
                          "%s tracks replies as %s-xxx but the reply recorder only "
                          "counts %s, so its leads go uncounted: add %r to "
                          "AD_REF_PREFIXES in %s."
                          % (row["slug"], prefix, ", ".join(sorted(known)), prefix,
                             AD_REFS.name)))
    return found
