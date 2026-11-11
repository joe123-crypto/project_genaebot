"""Per-group daily ad cap, derived from how active a group is.

How many ads a group used to get was an accident of how many promos happened to
list it: a group in six of the ten promos took six ads a day whether or not it
was busy enough to absorb them. This module gives every group a daily allowance
and decides, for one day, which campaigns may use it.

Two inputs:

* The **activity tier** the operator sets per group in the store
  (``busy`` / ``normal`` / ``quiet``) picks a band, e.g. ``busy -> 6..10``.
* The **ad-reply signal** the bot already records moves the cap inside that
  band. ``runtime/public-service-gate/ad-refs.json`` counts inbound DMs carrying
  an ad ref code (someone saw an ad in a group and tapped its wa.me link), and a
  ref's 3-character suffix is a pure function of the group JID
  (:func:`promo_broadcast.ref_code`), so replies are attributable per group. A
  group whose ads get answered earns its way up its band; one that goes quiet
  decays back to the floor, which is why the cap moves day to day.

Note what the signal is NOT: group chatter. The public-service-gate drops group
events before anything counts them, so there is no per-group message volume to
read -- the tier is the operator's judgement of that, and the replies modulate
it. Appliance-repairs contributes nothing either: its ``AR`` prefix is
deliberately absent from the plugin's ``AD_REF_PREFIXES`` because that ad links
to the repair shop's own number, so those replies never reach the bot. A group
only that promo targets therefore sits at its band floor.

When a group's cap is smaller than the number of promos targeting it,
:func:`allowed_promos` picks the day's winners by **rotation**: each promo's sort
key advances by one per day over a fixed circular order, so the selected set is a
sliding window and over ``len(promos)`` days every campaign gets exactly ``cap``
turns. It is a pure function of ``(day, jid, promo set, cap)`` -- identical in
every cron process, with no shared state and no lock, which matters because
:mod:`json_store` is last-writer-wins and the ten crons can overlap.

Std-lib only (plus the sibling :mod:`promo_broadcast` and :mod:`json_store`), so
it stays importable from the OpenClaw ``--command`` cron scripts.
"""

import os
from datetime import date, timedelta

import json_store
import promo_broadcast
import promo_membership

# Ref prefixes the bot actually receives replies for. Mirrors AD_REF_PREFIXES in
# workspace/plugins/public-service-gate/lib/ad-refs.js; "AR" is absent there on
# purpose (that ad links to the repair shop's number, not the bot's), so it is
# absent here too. deploy/test_promo_caps.py pins this list.
AD_REF_PREFIXES = ("BH", "BM", "CV", "FT", "GA", "GL", "GN", "SK")

AD_REFS_PATH = os.environ.get(
    "PROMO_AD_REFS_PATH",
    "/home/joseph/.openclaw/runtime/public-service-gate/ad-refs.json",
)
HISTORY_PATH = os.environ.get(
    "PROMO_CAPS_HISTORY",
    "/home/joseph/.openclaw/runtime/promo-caps/ad-ref-history.json",
)

# A reply is worth one extra ad per day above the band floor...
SCORE_PER_STEP = 1.0
# ...weighted by how recent the group's newest reply is, so a group that stops
# converting decays back to its floor instead of coasting on old wins.
RECENCY_WEIGHTS = ((7, 1.0), (14, 0.5), (30, 0.25))
# Most a cap may move in one day, so allowances drift instead of whipsawing.
MAX_CAP_STEP = 1
# Days of snapshots kept; enough to see a trend, small enough to stay tiny.
HISTORY_DAYS = 60


def _as_date(value):
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    try:
        return date(*(int(p) for p in text.split("-")))
    except (TypeError, ValueError):
        return None


def load_ad_refs(path=None):
    """``{ref: entry}`` from ad-refs.json, or ``{}`` when it is missing or
    unreadable. Fail-open on purpose: no signal means every group sits at its
    band floor, never that sending stops."""
    raw = json_store.load_json(path or AD_REFS_PATH, {})
    refs = raw.get("refs") if isinstance(raw, dict) else None
    return dict(refs) if isinstance(refs, dict) else {}


def recency_weight(last_seen, today):
    """How much a group's replies still count, by age of its newest one."""
    seen = _as_date(last_seen)
    if seen is None:
        return 0.0
    age = (today - seen).days
    if age < 0:
        age = 0
    for limit, weight in RECENCY_WEIGHTS:
        if age <= limit:
            return weight
    return 0.0


def ref_codes_for(jid, prefixes=AD_REF_PREFIXES):
    """Every ref code this group could appear under, one per promo prefix."""
    return [promo_broadcast.ref_code(prefix, jid) for prefix in prefixes]


def ad_ref_scores(jids, refs=None, today=None, prefixes=AD_REF_PREFIXES):
    """``{jid: score}`` -- recency-weighted unique repliers per group.

    ``refs`` is a parsed ad-refs mapping (default: read it). Unique senders are
    preferred over raw ``count`` so one persistent person cannot inflate a
    group's allowance."""
    today = today or date.today()
    refs = load_ad_refs() if refs is None else refs
    scores = {}
    for jid in jids:
        volume = 0
        newest = None
        for code in ref_codes_for(jid, prefixes):
            entry = refs.get(code)
            if not isinstance(entry, dict):
                continue
            senders = entry.get("uniqueSenders")
            if not isinstance(senders, int) or isinstance(senders, bool) or senders < 0:
                senders = 1 if entry.get("count") else 0
            volume += senders
            seen = _as_date(entry.get("lastSeen"))
            if seen and (newest is None or seen > newest):
                newest = seen
        scores[jid] = volume * recency_weight(newest, today) if volume else 0.0
    return scores


def target_cap(m, jid, score=0.0):
    """The cap ``score`` earns this group today, before smoothing.

    A ``daily_cap`` override wins outright; a group whose status is not
    ``active`` gets 0 (it is advertised to in no campaign); otherwise the band
    floor plus one step per weighted reply, clamped to the band ceiling."""
    entry = promo_membership.groups(m).get(jid)
    if entry is None:
        return None
    override = entry.get("daily_cap")
    if isinstance(override, int) and not isinstance(override, bool) and override >= 0:
        return override
    if not promo_membership.is_active(entry):
        return 0
    tier = promo_membership.activity_of(m, entry)
    low, high = promo_membership.bands(m).get(tier, promo_membership.DEFAULT_BANDS["normal"])
    earned = low + int(max(0.0, score) // SCORE_PER_STEP)
    return max(low, min(high, earned))


def smooth_cap(target, previous, step=MAX_CAP_STEP):
    """Keep a cap within ``step`` of yesterday's, so a single reply (or a quiet
    week) cannot swing a group's allowance far in one day. ``previous`` of
    ``None`` (no history yet) takes the target as-is."""
    if previous is None or target is None:
        return target
    if target > previous + step:
        return previous + step
    if target < previous - step:
        return previous - step
    return target


def rotation_order(jid, promos):
    """The group's fixed circular order of campaigns: a stable permutation keyed
    on ``(jid, promo)``, tie-broken by name so it is identical everywhere."""
    return sorted(promos or [],
                  key=lambda p: (promo_broadcast._stable_hash("%s:%s" % (jid, p)), p))


def allowed_promos(jid, promos, cap, day=None):
    """The promos that may post to ``jid`` on ``day``, as a list in the order
    ``promos`` was given. Fewer promos than the cap -> all of them.

    Today's winners are a window of ``cap`` entries starting at
    ``day.toordinal() % n`` of the group's :func:`rotation_order`. The window
    slides by exactly one per day over a true permutation, so across ``n`` days
    every campaign gets exactly ``cap`` turns -- no campaign can starve on a
    capped group, however its name sorts."""
    promos = list(promos or [])
    if cap is None or cap >= len(promos):
        return list(promos)
    if cap <= 0:
        return []
    day = day or date.today()
    order = rotation_order(jid, promos)
    n = len(order)
    start = day.toordinal() % n
    chosen = set(order[(start + i) % n] for i in range(cap))
    return [p for p in promos if p in chosen]


def snapshot_ad_refs(refs, today=None, path=None, keep=HISTORY_DAYS):
    """Record today's ad-ref counters so a future day can compare against them
    (ad-refs.json itself is cumulative, with no per-day history). Idempotent:
    the first promo run of the day writes it and the rest overwrite the same
    entry with the same data. Best-effort -- a failure here never affects
    sending."""
    today = today or date.today()
    path = path or HISTORY_PATH
    try:
        history = json_store.load_json(path, {})
        if not isinstance(history, dict):
            history = {}
        days = history.get("days")
        if not isinstance(days, dict):
            days = {}
        days[today.isoformat()] = dict(
            (code, {"uniqueSenders": entry.get("uniqueSenders"),
                    "count": entry.get("count"),
                    "lastSeen": entry.get("lastSeen")})
            for code, entry in (refs or {}).items() if isinstance(entry, dict)
        )
        for stale in sorted(days)[:-keep] if keep else []:
            days.pop(stale, None)
        history["version"] = 1
        history["days"] = days
        json_store.write_json_atomic(path, history)
        return history
    except OSError:
        return None


def previous_scores(jids, day, history=None, path=None):
    """Scores as of the most recent snapshot before ``day``, or ``None`` when
    there is no earlier snapshot to smooth against."""
    history = json_store.load_json(path or HISTORY_PATH, {}) if history is None else history
    days = history.get("days") if isinstance(history, dict) else None
    if not isinstance(days, dict):
        return None
    earlier = sorted(d for d in days if _as_date(d) and _as_date(d) < day)
    if not earlier:
        return None
    stamp = earlier[-1]
    return ad_ref_scores(jids, refs=days.get(stamp) or {}, today=_as_date(stamp))


def plan(m, jids, day=None, refs=None, history=None, record=False,
         disabled=None):
    """``{jid: info}`` for one day, where info is
    ``{"tier", "score", "cap", "promos", "allowed"}``.

    ``promos`` leaves out the campaigns in ``disabled``, so the rotation only hands
    a group's allowance to campaigns that will actually post.

    ``allowed`` is the promos that may post to that group today. Groups absent
    from membership.json are returned with ``cap=None`` and every promo allowed,
    so a JID that is targeted but not yet recorded is never silently dropped."""
    day = day or date.today()
    jids = list(jids)
    if refs is None:
        refs = load_ad_refs()
    if record:
        snapshot_ad_refs(refs, today=day)
    scores = ad_ref_scores(jids, refs=refs, today=day)
    prior = previous_scores(jids, day, history=history)
    # The caller supplies the off list. A campaign's switches live in Firestore
    # and only promo_config.resolve has read them; this module is handed the
    # membership, not a credential, so it must not guess -- and guessing
    # "nothing is off" would hand a paused campaign a group's daily turn.
    disabled = set(disabled or ())

    out = {}
    for jid in jids:
        entry = promo_membership.groups(m).get(jid)
        if entry is None:
            out[jid] = {"tier": None, "score": scores.get(jid, 0.0), "cap": None,
                        "promos": [], "allowed": None, "known": False}
            continue
        target = target_cap(m, jid, scores.get(jid, 0.0))
        before = None
        if prior is not None:
            before = target_cap(m, jid, prior.get(jid, 0.0))
        cap = smooth_cap(target, before)
        promos = [p for p in promo_membership.promos_for_group(m, jid)
                  if p not in disabled]
        out[jid] = {
            "tier": promo_membership.activity_of(m, entry),
            "score": scores.get(jid, 0.0),
            "cap": cap,
            "promos": promos,
            "allowed": allowed_promos(jid, promos, cap, day=day),
            "known": True,
        }
    return out


def filter_for_promo(m, promo, jids, day=None, refs=None, history=None, record=False,
                     disabled=None):
    """``(kept, decisions)``: the subset of ``jids`` that ``promo`` may post to
    today, plus the full per-group plan for logging. A group missing from
    membership.json is kept.

    ``promo`` itself is never treated as disabled here: whoever is running it
    (an operator's manual run or ``--dry-run`` of a paused campaign) gets the
    rotation it would have if it were switched on."""
    disabled = set(disabled or ()) - {promo}
    decisions = plan(m, jids, day=day, refs=refs, history=history, record=record,
                     disabled=disabled)
    kept = [j for j in jids
            if decisions[j]["allowed"] is None or promo in decisions[j]["allowed"]]
    return kept, decisions
