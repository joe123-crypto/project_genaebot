"""The live campaign list, for the two release gates that used to read files.

``deploy/promo-deploy-window.py`` holds a release until no campaign is sending,
and ``deploy/railway-release.py`` checks that the gateway came back up with every
enabled campaign's cron job. Both used to work entirely from the checkout: the
schedule and timeout from ``deploy/promo-crons.json``, the group count from each
campaign's ``targets.json``, the job name from the manifest entry.

That is operator data and it now lives only in Firestore, which CI has no
credentials for. So both gates ask the gateway, over the ``railway ssh`` the
deploy job is already authenticated for (see ``deploy/gateway-campaigns.sh``),
and read ``console-read campaigns`` -- the same read the console uses, which
reports every campaign whatever kind it is.

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

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "gateway-campaigns.sh"
# Generous: this is one ssh round trip into a container that may be cold, and
# the alternative to waiting is failing a release that was fine.
TIMEOUT_SECONDS = 180
# What a gate is allowed to act on. A campaign is only included when all of
# these are present and of the right type, because a gate that silently defaults
# a missing schedule to midnight is worse than one that says it cannot tell.
REQUIRED = ("slug", "enabled", "hour", "minute", "timezone", "targetCount",
            "timeoutSeconds")


class Unavailable(Exception):
    """The live campaign list could not be read. Never means "no campaigns"."""


def _run(script=None, timeout=TIMEOUT_SECONDS, env=None, runner=None):
    runner = runner or subprocess.run
    try:
        done = runner([str(script or SCRIPT)], capture_output=True, text=True,
                      timeout=timeout, env=dict(os.environ if env is None else env))
    except FileNotFoundError:
        raise Unavailable("%s is missing" % (script or SCRIPT))
    except subprocess.TimeoutExpired:
        raise Unavailable("the gateway did not answer within %d seconds" % timeout)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise Unavailable("asking the gateway failed (exit %d): %s"
                          % (done.returncode, detail[-1] if detail else "no output"))
    return done.stdout


def parse(text):
    """The campaign rows in one ``console-read campaigns`` reply.

    ``console-read`` reports a read it could not complete as ``{"error": ...}``
    with a zero exit, so an error field is checked for before the rows."""
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
    if not out:
        # Every campaign disabled is normal; none existing at all means this is
        # not the gateway the release is about to replace.
        raise Unavailable("the gateway reports no campaigns at all")
    return out


def read(**inject):
    """Every campaign the gateway knows, as ``console-read`` reports them."""
    return parse(_run(**inject))


def enabled(rows):
    return [row for row in rows if row.get("enabled")]


def job_names(rows):
    """The cron jobs an enabled campaign must have, for the startup check.

    A campaign converted from a script keeps its original ``<slug>-promo`` job;
    one created in the console has ``console-<slug>-promo``. Either way the
    gateway reports the name, so this never has to guess it."""
    names = set()
    for row in enabled(rows):
        name = row.get("jobName")
        if not isinstance(name, str) or not name:
            raise Unavailable("campaign %r has no job name" % row.get("slug"))
        names.add(name)
    return names
