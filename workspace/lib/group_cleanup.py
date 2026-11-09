"""Forget what the bot stored about one group.

Deleting a group's config record says nothing about the per-campaign state the
cron jobs keep beside it. Each campaign remembers the day it last posted to
every group in ``runtime/cron-state/<name>/state.json``::

    {"sent": {"263786627810-1599925488@g.us": "2026-08-20"}, "resolved": {}}

An entry for a group that no longer exists is harmless to a run -- nothing looks
it up -- but it is a record of a group the operator asked to be forgotten, and
it would quietly decide the first send if the same group were ever re-added. So
a delete can ask for it to go.

What this deliberately leaves alone
-----------------------------------
``runtime/public-service-gate/ad-refs.json`` and
``runtime/promo-caps/ad-ref-history.json`` are **not** touched. Three reasons,
in order of weight:

* they are keyed by ref code, not by jid (``promo_caps.ref_codes_for``), and a
  ref code is a pure function of the jid, so clearing them would also erase the
  reply history of any future group that hashes to the same code;
* they are written live by the public-service-gate plugin, in another process,
  with no lock this could take -- editing them from here races a writer;
* they are evidence that somebody replied to an advert. ``ad_ref_scores`` only
  ever looks up the jids it is given, so an entry for a departed group is inert.

Std-lib only, so it can be imported from the console bridges.
"""

import os

import json_store

DEFAULT_STATE_DIR = "/home/joseph/.openclaw"
# The two maps in a campaign's state file that are keyed by group jid.
GROUP_KEYED = ("sent", "resolved")


def cron_state_dir(env=None):
    """``runtime/cron-state`` on the volume, where every campaign keeps its own
    ``<name>/state.json``."""
    env = os.environ if env is None else env
    root = env.get("OPENCLAW_STATE_DIR") or DEFAULT_STATE_DIR
    return os.path.join(root, "runtime", "cron-state")


def clear_group(jid, env=None):
    """Remove ``jid`` from every campaign's state file.

    Returns ``{"campaigns": {name: [map names cleared]}, "checked": n}``. A
    campaign that never posted to the group is simply absent from ``campaigns``.

    Nothing here raises for a missing, unreadable or unexpected state file: the
    group is being forgotten, and a state file this cannot parse is one no
    campaign could have read either. The caller reports what was cleared, so a
    file that was skipped shows up as an absence rather than a false claim.
    """
    if not isinstance(jid, str) or not jid:
        raise ValueError("jid must be a non-empty string")
    directory = cron_state_dir(env)
    cleared, checked = {}, 0
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return {"campaigns": {}, "checked": 0}
    for name in names:
        path = os.path.join(directory, name, "state.json")
        if not os.path.isfile(path):
            continue
        checked += 1
        state = json_store.load_json(path, None)
        if not isinstance(state, dict):
            continue
        removed = []
        for key in GROUP_KEYED:
            value = state.get(key)
            if isinstance(value, dict) and jid in value:
                del value[jid]
                removed.append(key)
        if not removed:
            continue
        try:
            json_store.write_json_atomic(path, state)
        except OSError:
            continue
        cleared[name] = removed
    return {"campaigns": cleared, "checked": checked}
