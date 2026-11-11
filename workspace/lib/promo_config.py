"""The operator config a campaign run acts on, read once per run from Firestore.

There used to be a choice here, behind ``GENAIE_CONFIG_SOURCE``: the repository's
``membership.json`` and generated ``targets.json`` files, or Firestore, with a
shadow mode that read the files and compared them against the store. The files
are gone -- they were operator data in a code repository -- so the choice is
gone with them and this module always reads the store.

:func:`resolve` answers the question once per run and hands back everything the
run needs: the targets payload, the membership the caps derive from, and each
campaign's on/off switch. Resolving once matters: the engine used to open the
targets file five times, so a config change landing mid-run could split one run
across two configs.

**It fails closed.** An outage serves the last-known-good snapshot from
``runtime/firestore/``; too old a snapshot, or none, and :func:`resolve` raises
:class:`operator_config.ConfigUnavailable` and the caller must send nothing.
There is deliberately no second source to fall back to: the one that existed
was a committed file, and by the time it was deleted it had drifted far enough
from the store that falling back to it would have re-posted into groups someone
had paused and advertised to groups that had been removed.

Std-lib only, so it stays importable from the OpenClaw ``--command`` cron
scripts. ``promo_membership`` is imported lazily: it imports ``promo_broadcast``,
which imports this module.
"""

import datetime
import time
from zoneinfo import ZoneInfo
import os
import sys

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import operator_config  # noqa: E402

# Kept because the runtime still sets it and an operator who flips it back to
# "file" expecting a rollback deserves to be told that there is nothing to roll
# back to, rather than to watch a campaign send from the store anyway in silence.
MODE_VAR = "GENAIE_CONFIG_SOURCE"
FIRESTORE = "firestore"
PROMO_DIR_SUFFIX = "-promo"
PREFIX = "[operator-source]"


def retired_mode(env=None):
    """A warning when ``GENAIE_CONFIG_SOURCE`` asks for a source that is gone."""
    env = os.environ if env is None else env
    raw = (env.get(MODE_VAR) or "").strip().lower()
    if raw in ("", FIRESTORE):
        return None
    return ("%s=%r no longer selects anything: the committed membership and "
            "targets files have been deleted, so Firestore is the only source. "
            "Unset it." % (MODE_VAR, env.get(MODE_VAR)))


def promo_key(promo_name):
    """``"cv-promo"`` -> ``"cv"``: the slug a campaign is stored under."""
    name = str(promo_name or "")
    if name.endswith(PROMO_DIR_SUFFIX):
        return name[:-len(PROMO_DIR_SUFFIX)]
    return name


def resolve(promo, env=None, err=None, config_loader=None, **inject):
    """What this run acts on::

        {"source": "firestore" | "snapshot",
         "mode": "firestore",
         "doc": <targets.json-shaped dict>,
         "membership": <membership-shaped dict>,
         "switches": {slug: bool},         # on only if BOTH switches are on
         "storeSwitches": {slug: bool},    # the operator's pause switch alone
         "cronSwitches": {slug: bool},     # whether a verified cron exists
         "enabled": True | False | None,   # this promo's own switch in the store
                                           # (what gates the run); None = unknown
         "report": {...},                  # quarantine/warnings
         "notes": [str]}

    ``promo`` may be the campaign slug or the ``<slug>-promo`` job name. Raises
    :class:`operator_config.ConfigUnavailable` when there is no trustworthy
    config, which is the only correct answer: a run that cannot read the config
    must not guess where to advertise."""
    import promo_membership  # deferred: promo_membership imports promo_broadcast

    env = os.environ if env is None else env
    err = err or sys.stderr
    slug = promo_key(promo)
    notes = []

    def note(message):
        notes.append(message)
        err.write("%s %s\n" % (PREFIX, message))

    warning = retired_mode(env)
    if warning:
        note(warning)

    if config_loader is None and slug:
        loaded = operator_config.load(env, log=note, campaign=slug, **inject)
    else:
        loaded = (config_loader or operator_config.load)(env, log=note, **inject)
    membership = loaded["membership"]
    # Two switches, and a campaign is on only if both are: the operator's pause
    # control in the store, and whether the gateway actually has a verified cron
    # to fire it. ``switches`` combines them, so the caps never hand a group's
    # allowance to a campaign that cannot post either way. ``enabled`` stays the
    # store switch alone: it is the live pause control, and it gates this run.
    cron = {}
    switches = {}
    campaign_day = datetime.datetime.fromtimestamp(
        inject.get("now", time.time)(), ZoneInfo("Africa/Harare")).date()
    for key, meta in (membership.get("promos") or {}).items():
        import console_campaigns
        ready = (meta.get("scheduleStatus") == "ready"
                 and console_campaigns.schedule_occurrence(meta, campaign_day) is not None)
        cron[key] = bool(ready)
        switches[key] = bool(loaded["enabled"].get(key)) and bool(ready)
    # A campaign in the switch map with no document is already off everywhere;
    # carry it so a caller iterating switches sees the same keys it always did.
    for key, on in loaded["enabled"].items():
        switches.setdefault(key, bool(on) and cron.get(key, False))
        cron.setdefault(key, False)
    # A caller that names no campaign (the console's read model) wants the
    # membership and the switches, not one campaign's target list.
    doc = promo_membership.targets_payload(membership, slug) if slug else {}
    if loaded["degraded"]:
        err.write("%s serving the snapshot taken %s: %s\n"
                  % (PREFIX, loaded.get("fetchedAt"), loaded["degraded"]))
    return {"source": loaded["source"], "mode": FIRESTORE, "doc": doc,
            "membership": membership, "switches": switches,
            "storeSwitches": dict(loaded["enabled"]), "cronSwitches": cron,
            "enabled": loaded["enabled"].get(slug), "report": loaded["report"],
            "deleted": loaded.get("deleted", []), "notes": notes}
