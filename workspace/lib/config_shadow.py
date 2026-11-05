#!/usr/bin/env python3
"""Shadow mode: read operator config from Firestore beside membership.json, act
on the file, and record every difference.

This is phase 2 of moving operator config into Firestore (see
``operator_config``). Before production acts on Firestore it has to agree with
the file through days of real runs, not just on the day it was seeded. Shadow
mode is how that is checked without Firestore ever deciding where an ad goes.

Switched by one variable:

    GENAIE_CONFIG_SOURCE   unset or "file"  shadow mode off (today's behaviour)
                           "shadow"         every campaign run also reads Firestore

Any other value is reported and treated as "file". In particular "firestore",
the phase 3 value, is NOT honoured yet: acting on Firestore needs the cutover
code, and a variable set early must not pretend it arrived.

In shadow mode each campaign run starts the read in a background thread and
collects the result as it finishes, so a slow or unreachable Firestore costs
the run at most ``JOIN_SECONDS`` at the end and never delays a send. Whatever
happens -- no key, a 403, a hang, a bug in this module -- the run sends exactly
what it would have sent without shadow mode. Output goes to stderr only; a cron
script's stdout is its reply.

Each check writes its result to ``runtime/firestore/shadow.json`` (the newest
check wins) and console-read reports it, so drift shows up in the Genaie
console instead of only in run logs. The read itself goes through
``operator_config.load``, which keeps the last-known-good snapshot and the
change log current: by cutover the snapshot the fail-closed path depends on has
been exercised for days.

Run by hand for a one-off check (prints the result, exits 1 on drift):

    python3 workspace/lib/config_shadow.py

Std-lib only, Python 3.11+.
"""

import datetime
import functools
import json
import os
import sys
import threading
import time
from pathlib import Path

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import operator_config  # noqa: E402

MODE_VAR = "GENAIE_CONFIG_SOURCE"
FILE, SHADOW = "file", "shadow"
RESULT_FILE = "shadow.json"
# How long a finished run waits for a check still in flight. Normally the check
# is done long before the last send; this bounds the cost of a hung Firestore
# on a run with nothing to send. operator_config's own timeouts are longer, so
# a check that misses this deadline is reported as unfinished, not waited for.
JOIN_SECONDS = 10.0
# The cron manifest, where each campaign's on/off switch is declared today. The
# image bakes a copy outside deploy/, which is never staged into the container.
WORKSPACE = Path(LIB).parent
MANIFEST_CANDIDATES = (
    str(WORKSPACE.parent / "deploy" / "promo-crons.json"),
    "/usr/local/lib/genaie/promo-crons.json",
)
PREFIX = "[config-shadow]"


def mode(env=None):
    """``(mode, warning)``: ``"file"`` or ``"shadow"``, plus a message when the
    variable holds something this phase does not act on."""
    env = os.environ if env is None else env
    raw = (env.get(MODE_VAR) or "").strip().lower()
    if raw in ("", FILE):
        return FILE, None
    if raw == SHADOW:
        return SHADOW, None
    return FILE, ("%s=%r is not supported yet; acting on membership.json and "
                  "skipping the shadow check" % (MODE_VAR, env.get(MODE_VAR)))


def result_path(env=None):
    return os.path.join(operator_config.state_dir(env), RESULT_FILE)


def file_config(env=None, membership_path=None):
    """What production acts on today: membership.json plus the manifest's switches."""
    import promo_membership

    env = os.environ if env is None else env
    membership = promo_membership.load(membership_path)
    if membership is None:
        raise operator_config.ConfigInvalid(
            "membership file not found: %s" % (membership_path or promo_membership.default_path()))
    override = env.get("GENAIE_PROMO_CRONS")
    candidates = [override] if override else list(MANIFEST_CANDIDATES)
    for candidate in candidates:
        try:
            with open(candidate, encoding="utf-8") as handle:
                manifest = json.load(handle)
        except FileNotFoundError:
            continue
        except ValueError as ex:
            raise operator_config.ConfigInvalid("%s is not valid JSON: %s" % (candidate, ex))
        return membership, operator_config.manifest_switches(manifest)
    raise operator_config.ConfigInvalid("no cron manifest at: %s" % ", ".join(candidates))


def _stamp(now):
    at = datetime.datetime.fromtimestamp(now(), datetime.timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def check(env=None, membership_path=None, *, opener=None, signer=None, now=None,
          sleep=None):
    """Compare Firestore with the file once. Never raises for anything Firestore
    or the data does; returns the result it also writes to ``shadow.json``::

        {"checkedAt": ISO-8601 UTC,
         "status": "exact" | "drift" | "unavailable" | "error",
         "source": "firestore" | "snapshot" | None,  # what the comparison used
         "degraded": None | why Firestore itself was not read,
         "problems": [...],     # parity differences, quarantine included
         "notes": [...]}        # what operator_config logged while reading

    ``exact`` from a snapshot is still reported with its source: the snapshot
    matching proves less than a live read matching, and the console says so."""
    env = os.environ if env is None else env
    now = now or time.time
    notes = []
    result = {"checkedAt": _stamp(now), "status": "error", "source": None,
              "degraded": None, "problems": [], "notes": notes}
    try:
        expected, expected_switches = file_config(env, membership_path)
        try:
            got = operator_config.load(env, opener=opener, signer=signer, now=now,
                                       sleep=sleep, log=notes.append)
        except operator_config.ConfigUnavailable as ex:
            result.update(status="unavailable", degraded=str(ex))
        else:
            result["source"] = got["source"]
            result["degraded"] = got["degraded"]
            problems = ["quarantined %s: %s" % (jid, "; ".join(found))
                        for jid, found in sorted(got["report"]["quarantined"].items())]
            problems += ["campaign %s treated as off: %s" % (slug, problem)
                         for slug, problem in sorted(got["report"].get("campaigns", {}).items())]
            problems += operator_config.parity_diff(expected, expected_switches,
                                                    got["membership"], got["enabled"])
            result["problems"] = problems
            result["status"] = "drift" if problems else "exact"
    except operator_config.ConfigInvalid as ex:
        # The file side is broken: there is nothing trustworthy to compare with.
        result["problems"] = ["file side unreadable: %s" % ex]
    except Exception as ex:  # a shadow check must never take a run down
        result["problems"] = ["shadow check failed: %s: %s" % (type(ex).__name__, ex)]
    try:
        operator_config._write_private_json(result_path(env), result)
    except OSError as ex:
        notes.append("could not write %s: %s" % (result_path(env), ex))
    return result


def summary(result):
    """Lines for a run log: one headline, then each difference."""
    status = result.get("status")
    source = result.get("source")
    if status == "exact":
        head = "Firestore matches membership.json"
        if source != "firestore":
            head += " (compared with the %s: %s)" % (source, result.get("degraded"))
    elif status == "drift":
        head = "Firestore differs from membership.json in %d place(s)%s" % (
            len(result["problems"]),
            "" if source == "firestore" else " (compared with the %s)" % source)
    elif status == "unavailable":
        head = "Firestore unreadable and no snapshot: %s" % result.get("degraded")
    else:
        head = "shadow check could not run"
    lines = ["%s %s; this run used membership.json" % (PREFIX, head)]
    lines += ["%s   %s" % (PREFIX, p) for p in result.get("problems") or []]
    lines += ["%s   note: %s" % (PREFIX, n) for n in result.get("notes") or []]
    return lines


class _Check:
    def __init__(self, thread, box):
        self.thread = thread
        self.box = box


def start(env=None, membership_path=None, err=None, **inject):
    """Begin a background check when shadow mode is on; ``None`` otherwise."""
    env = os.environ if env is None else env
    err = err or sys.stderr
    try:
        current, warning = mode(env)
        if warning:
            err.write("%s %s\n" % (PREFIX, warning))
        if current != SHADOW:
            return None
        box = {}

        def run():
            box["result"] = check(env, membership_path, **inject)

        thread = threading.Thread(target=run, name="config-shadow", daemon=True)
        thread.start()
        return _Check(thread, box)
    except Exception as ex:
        err.write("%s could not start: %s: %s\n" % (PREFIX, type(ex).__name__, ex))
        return None


def finish(handle, timeout=JOIN_SECONDS, err=None):
    """Report a check started by :func:`start`. Never raises."""
    if handle is None:
        return None
    err = err or sys.stderr
    try:
        handle.thread.join(timeout)
        result = handle.box.get("result")
        if result is None:
            err.write("%s check still running after %.0f s; not waiting for it. "
                      "This run used membership.json.\n" % (PREFIX, timeout))
            return None
        for line in summary(result):
            err.write(line + "\n")
        return result
    except Exception as ex:
        err.write("%s could not report: %s: %s\n" % (PREFIX, type(ex).__name__, ex))
        return None


def alongside(function):
    """Run a shadow check beside a campaign run. The run's own result, return
    value and exceptions pass through untouched."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        handle = start(membership_path=kwargs.get("membership_path"))
        try:
            return function(*args, **kwargs)
        finally:
            finish(handle)
    return wrapped


def read_result(env=None):
    """The newest check, or ``None`` when shadow mode has never run here."""
    try:
        with open(result_path(env), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def main(argv):
    result = check()
    for line in summary(result):
        print(line)
    return 0 if result["status"] == "exact" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
