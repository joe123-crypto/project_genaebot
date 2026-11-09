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
# `sent` is keyed by group jid: {"<jid>": "2026-08-20"}.
SENT = "sent"
# `resolved` is NOT. It caches invite-link lookups the other way round --
# {"https://chat.whatsapp.com/<code>": "<jid>"} (promo_broadcast.resolve_targets)
# -- so a group is found there by VALUE. Left behind, the cache would hand a
# later run the jid of a group the bot has been told to forget.
RESOLVED = "resolved"


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
        removed = []

        def forget(state, removed=removed):
            """Applied to the state as it is INSIDE the lock, so a send
            committed since this sweep began is still here to be kept."""
            if not isinstance(state, dict):
                return False
            sent = state.get(SENT)
            if isinstance(sent, dict) and jid in sent:
                del sent[jid]
                removed.append(SENT)
            resolved = state.get(RESOLVED)
            if isinstance(resolved, dict):
                invites = [invite for invite, target in resolved.items()
                           if target == jid]
                for invite in invites:
                    del resolved[invite]
                if invites:
                    removed.append(RESOLVED)
            return bool(removed)

        try:
            changed = json_store.locked_update_json(path, forget)
        except OSError:
            continue
        if changed:
            cleared[name] = removed
    return {"campaigns": cleared, "checked": checked}
