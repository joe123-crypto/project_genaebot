"""Shared delivery engine for the deterministic WhatsApp promo crons.

``cron/genaie-promo``, ``cron/building-materials-promo``, and ``cron/cv-promo``
each post a FIXED advert (caption + image) to a set of WhatsApp groups, once per
day per group, via ``openclaw message send --media``, recording a group as "sent
today" only after a confirmed send so delivery is transactional per group. They
historically carried an identical copy of the gateway-env setup, invite/JID
resolution, per-group state, the permission-error skip, and the send loop. That
logic lives here now; each promo script keeps only its advert text, image(s), and
file paths and calls :func:`broadcast`.

Behavior is preserved exactly, including the ``--dry-run`` output and the optional
per-group weekly cadence (a group listed under ``weekly_groups`` in targets.json
is posted to only on its configured weekdays; groups only under the flat
``groups`` list are posted to daily). Weekly support is inert when a targets file
has no ``weekly_groups`` key, so it is transparent for the daily-only promos.

Caption rotation: ``text`` may be a single string (sent verbatim, unchanged
behavior) or a list of caption variants. A group's variant is
``(post counter + stable hash of the group JID) % len(variants)``: the counter
advances by one per post (per day for daily groups, per configured weekday for
weekly-limited groups), so a group never gets the identical caption on two
consecutive posts, and the per-JID offset spreads groups across variants on any
given day. The hash is ``hashlib.sha256`` -- NOT Python's per-process salted
``hash()`` -- so the choice is identical across runs and retries.

Tracking links (opt-in per promo via ``tracking=``): each group gets a short,
stable ref code (promo prefix + 3 chars derived from the group JID, e.g.
``BH-7K2``) inside a ``wa.me`` click-to-chat link whose prefilled text names the
ad and the ref. A caption may place it with ``{ref_link}`` / ``{ref}``
placeholders; otherwise a "tap to chat" line is appended. A promo that passes no
``tracking`` sends exactly the caption it supplies. ``--refs`` prints the
ref -> promo + group JID mapping (no sends, no state mutation) so codes seen in
DMs can be decoded later.

Send spacing: consecutive group sends are spaced by a random gap drawn
uniformly from [``send_delay_min``, ``send_delay_max``] seconds (default 20-60,
env ``PROMO_SEND_DELAY_MIN`` / ``PROMO_SEND_DELAY_MAX``) instead of a fixed
delay, because WhatsApp spam detection reportedly flags machine-regular send
intervals. The gap is only taken between two send attempts: never before the
first or after the last, and never for groups that are not sent to (already
sent today, off their weekly day) or on ``--dry-run`` / ``--refs``. Because a
promo cron's job timeout lives in the gateway's cron state on the volume rather
than in this repo, the engine asks the gateway for its own job's timeout and
tightens the gaps (keeping them random) when the run would not fit, so a stale
timeout delays groups instead of dropping them.

Std-lib only (plus the sibling :mod:`json_store` helper), so it stays importable
from the OpenClaw ``--command`` cron scripts, which are std-lib only by contract.
"""

import os
import sys
import json
import time
import math
import random
import hashlib
import subprocess
import urllib.parse
from datetime import date

import json_store


# ------------------------------------------------------------------ gateway ----
def gateway_env():
    """Env for nested ``openclaw`` calls so they reach the live gateway's WhatsApp
    channel. A bare ``openclaw message send`` runs standalone and reports
    "Channel is unavailable: whatsapp"; it must connect to the running gateway as
    a client, which needs OPENCLAW_GATEWAY_TOKEN set and OPENCLAW_HOME unset (same
    wrapper as workspace/bin/job-scout-agent)."""
    env = dict(os.environ)
    env.pop("OPENCLAW_HOME", None)
    token = os.environ.get("GATEWAY_AUTH_TOKEN")
    if not token:
        sys.stderr.write(
            "ERROR: GATEWAY_AUTH_TOKEN is not set; the WhatsApp channel is only "
            "reachable as a gateway client, so sends will fail.\n"
        )
    else:
        env["OPENCLAW_GATEWAY_TOKEN"] = token
    return env


# ---------------------------------------------------------- weekly cadence ----
# Some groups get the ad only on specific weekdays (e.g. twice a week) instead of
# daily. weekday() uses Mon=0 .. Sun=6. Weekday tokens in targets.json may be
# names ("mon", "monday", "thu", ...) or ints (0-6).
_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_weekday(token):
    """Return a weekday int 0-6 for a name or int token, else None."""
    if isinstance(token, bool):
        return None
    if isinstance(token, int):
        return token if 0 <= token <= 6 else None
    if isinstance(token, str):
        t = token.strip().casefold()
        if t in _WEEKDAY_NAMES:
            return _WEEKDAY_NAMES[t]
        if t.isdigit():
            n = int(t)
            return n if 0 <= n <= 6 else None
    return None


# ------------------------------------------------------------------ targets ----
def load_targets(targets_file):
    """Return the list of raw target entries (JIDs and/or invite links)."""
    try:
        with open(targets_file) as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.stderr.write("ERROR: targets file not found: %s\n" % targets_file)
        return []
    except json.JSONDecodeError as ex:
        sys.stderr.write("ERROR: targets file is not valid JSON: %s\n" % ex)
        return []
    groups = data.get("groups", []) if isinstance(data, dict) else data
    out = []
    for g in groups or []:
        if isinstance(g, str) and g.strip():
            out.append(g.strip())
    return out


def load_weekly_targets(targets_file):
    """Return {jid: frozenset(weekday ints)} for groups posted to only on certain
    weekdays. A JID listed here is posted to ONLY on its weekdays; a JID absent
    from this map (i.e. only in "groups") is posted to every day. Entries with no
    valid weekday, or a bad JID, are ignored."""
    try:
        with open(targets_file) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("weekly_groups") or {}
    out = {}
    if isinstance(raw, dict):
        for jid, days in raw.items():
            jid = str(jid).strip()
            if not jid.endswith("@g.us"):
                continue
            if isinstance(days, (str, int)) and not isinstance(days, bool):
                days = [days]
            parsed = set()
            for token in days or []:
                d = _parse_weekday(token)
                if d is not None:
                    parsed.add(d)
            if parsed:
                out[jid] = frozenset(parsed)
    return out


def is_jid(entry):
    return entry.endswith("@g.us")


def is_invite(entry):
    return "chat.whatsapp.com/" in entry


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _group_jid(group):
    for key in ("id", "groupId", "jid", "target", "chatId"):
        value = str(group.get(key) or "").strip()
        if value.endswith("@g.us"):
            return value
    return ""


def resolve_invite(entry):
    """Resolve an invite link to a group JID by matching its code against the
    OpenClaw WhatsApp directory (same approach as deploy/install-job-scout-group-feed.sh).
    The bot must be a member of the group for it to appear in the directory.
    Returns JID or None."""
    code = entry.rstrip("/").split("/")[-1].casefold()
    if not code:
        return None
    cmd = ["openclaw", "directory", "groups", "list",
           "--channel", "whatsapp", "--json", "--limit", "200"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           env=gateway_env())
    except Exception as ex:
        sys.stderr.write("WARN: directory list exec error for %s: %s\n" % (entry, ex))
        return None
    if p.returncode != 0:
        sys.stderr.write(
            "WARN: could not list groups to resolve %s:\n%s\n"
            % (entry, (p.stdout or "") + (p.stderr or ""))
        )
        return None
    # The command may print log lines before the JSON payload; find the first
    # line from which the remainder parses as JSON.
    payload = None
    lines = (p.stdout or "").splitlines()
    for i in range(len(lines)):
        candidate = "\n".join(lines[i:]).strip()
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if payload is None:
        sys.stderr.write("WARN: no JSON group list to resolve %s\n" % entry)
        return None
    matches = set()
    for group in _walk(payload):
        jid = _group_jid(group)
        if not jid:
            continue
        haystack = "\n".join(_strings(group)).casefold()
        if code in haystack:
            matches.add(jid)
    if len(matches) == 1:
        return next(iter(matches))
    if not matches:
        sys.stderr.write(
            "WARN: could not resolve %s (bot not a member, or invite code not in "
            "directory metadata)\n" % entry
        )
    else:
        sys.stderr.write(
            "WARN: invite %s matched multiple groups %s; set the JID explicitly\n"
            % (entry, sorted(matches))
        )
    return None


def resolve_targets(entries, state, dry=False):
    """Map raw entries to group JIDs, caching invite resolutions in state."""
    resolved_cache = state.setdefault("resolved", {})
    jids = []
    seen = set()
    for entry in entries:
        if is_jid(entry):
            jid = entry
        elif is_invite(entry):
            jid = resolved_cache.get(entry)
            if not jid:
                jid = resolve_invite(entry)
                if jid and not dry:
                    resolved_cache[entry] = jid
        else:
            sys.stderr.write("WARN: skipping unrecognized target: %s\n" % entry)
            continue
        if jid and jid not in seen:
            seen.add(jid)
            jids.append(jid)
    return jids


# -------------------------------------------------------------------- state ----
def load_state(state_file):
    st = json_store.load_json(state_file, {})
    st.setdefault("sent", {})
    st.setdefault("resolved", {})
    return st


def save_state(state_file, st):
    json_store.write_json_atomic(state_file, st)


# --------------------------------------------------------------------- send ----
# A send can fail because the bot lacks permission to post (e.g. an admins-only
# "announcement" group where the bot is not an admin). That is EXPECTED and
# permanent, not a service fault, so it must not fail the daily cron. WhatsApp/
# Baileys surfaces this as a forbidden / 403 / not-authorized error; match those
# markers to treat the send as a skip rather than a failure.
PERMISSION_MARKERS = (
    "forbidden",
    "not-authorized",
    "not authorized",
    "403",
    "only admins",
    "admins can",
    "not an admin",
    "not admin",
    "restricted",
    "announcement",
)


def is_permission_error(out):
    low = (out or "").casefold()
    return any(marker in low for marker in PERMISSION_MARKERS)


def send_ad(target, text, image_path):
    """Send the ad (caption + image) to one group. Returns (ok, combined_output)."""
    cmd = ["openclaw", "message", "send", "--channel", "whatsapp",
           "--target", target, "--message", text,
           "--media", image_path, "--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           env=gateway_env())
    except Exception as ex:
        return (False, "exec error: %s" % ex)
    out = (p.stdout or "") + (p.stderr or "")
    ok = (p.returncode == 0) and ('"messageId"' in out) and ('"error"' not in (p.stdout or ""))
    return (ok, out)


# --------------------------------------------------------------------- run ----
def _resolve_image(image):
    """``image`` is either a single path (fixed) or a list of paths that rotate
    daily by date ordinal so a group never sees the same image two days running.
    Returns (path_or_None, rotating)."""
    if isinstance(image, (list, tuple)):
        assets = [a for a in image if a]
        if not assets:
            return (None, True)
        return (assets[date.today().toordinal() % len(assets)], True)
    return (image, False)


# --------------------------------------------------------- caption rotation ----
def _stable_hash(value):
    """Deterministic non-negative int for ``value``. Stable across processes,
    unlike the builtin ``hash()`` which is salted per interpreter run."""
    return int(hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12], 16)


def _variants(text):
    """Normalize ``text`` to a non-empty list of caption variants. A plain string
    is a single fixed caption (the historical behavior)."""
    if isinstance(text, (list, tuple)):
        out = [t for t in text if isinstance(t, str) and t.strip()]
        if not out:
            raise ValueError("caption variant list is empty")
        return out
    return [text]


def _post_counter(day, allowed=None):
    """Per-group counter that advances by exactly one per post. For a daily group
    it is the date ordinal. For a weekly-limited group (``allowed`` weekdays) it
    counts only its allowed weekdays, so successive POSTS (e.g. Mon -> Thu -> Mon)
    still step by one and never repeat a caption back to back."""
    ordinal = day.toordinal()
    if not allowed:
        return ordinal
    days = sorted(allowed)
    week, weekday = divmod(ordinal - 1, 7)  # ordinal 1 (0001-01-01) is a Monday
    return week * len(days) + sum(1 for d in days if d < weekday)


def variant_index(jid, n_variants, day=None, allowed=None):
    """Index of the caption variant ``jid`` gets on ``day`` (default today)."""
    if n_variants <= 1:
        return 0
    day = day or date.today()
    return (_post_counter(day, allowed) + _stable_hash(jid)) % n_variants


# ------------------------------------------------------------ tracking refs ----
# Crockford base32 (no I, L, O, U): unambiguous when read off a phone screen.
REF_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
REF_CODE_LEN = 3
DEFAULT_PREFILL = "Hi, I saw your {label} ad (ref {ref})"
DEFAULT_LINK_LINE = "\U0001F449 Tap to chat on WhatsApp: {ref_link}"


def ref_code(prefix, jid):
    """Short stable ref for (promo prefix, group JID), e.g. ``BH-7K2``. The suffix
    depends only on the JID, so one group carries the same suffix in every promo
    while the prefix names the promo."""
    n = int(hashlib.sha256(str(jid).encode("utf-8")).hexdigest(), 16)
    chars = []
    for _ in range(REF_CODE_LEN):
        n, r = divmod(n, len(REF_ALPHABET))
        chars.append(REF_ALPHABET[r])
    return "%s-%s" % (str(prefix).upper(), "".join(chars))


def wa_digits(number):
    """``+263 78 563 2943`` -> ``263785632943`` (wa.me wants digits only)."""
    return "".join(ch for ch in str(number) if ch.isdigit())


def ref_link(number, prefill):
    """wa.me click-to-chat link to ``number`` with URL-encoded prefilled text."""
    return "https://wa.me/%s?text=%s" % (
        wa_digits(number), urllib.parse.quote(prefill, safe=""))


def tracking_for(tracking, jid):
    """(ref, link) for ``jid`` under a tracking config, or (None, None)."""
    if not tracking:
        return (None, None)
    ref = ref_code(tracking["prefix"], jid)
    prefill = (tracking.get("prefill") or DEFAULT_PREFILL).replace(
        "{label}", tracking.get("label", "")).replace("{ref}", ref)
    return (ref, ref_link(tracking["number"], prefill))


def render_caption(caption, ref=None, link=None, link_line=DEFAULT_LINK_LINE):
    """Substitute ``{ref_link}`` / ``{ref}`` in ``caption``. Uses plain
    ``str.replace`` (not ``str.format``), so any other braces in operator copy
    are left alone. With tracking on and no ``{ref_link}`` placeholder in the
    caption, ``link_line`` is appended after a blank line. With no ref/link the
    caption is returned unchanged."""
    if not ref and not link:
        return caption
    had_link = "{ref_link}" in caption
    out = caption.replace("{ref_link}", link or "").replace("{ref}", ref or "")
    if link and not had_link and link_line:
        line = link_line.replace("{ref_link}", link).replace("{ref}", ref or "")
        out = out.rstrip("\n") + "\n\n" + line
    return out


def caption_for(jid, variants, day=None, allowed=None, tracking=None):
    """(variant_index, rendered caption, ref) for one group on ``day``."""
    idx = variant_index(jid, len(variants), day=day, allowed=allowed)
    ref, link = tracking_for(tracking, jid)
    line = (tracking or {}).get("link_line") or DEFAULT_LINK_LINE
    return (idx, render_caption(variants[idx], ref, link, line), ref)


def ref_map(jids, tracking, promo_name):
    """[(ref, promo_name, jid, link)] for decoding ref codes later."""
    if not tracking:
        return []
    return [(ref, promo_name, j, link)
            for j in jids for ref, link in [tracking_for(tracking, j)]]


def _print_refs(jids, tracking, promo_name):
    if not tracking:
        print("Tracking is not enabled for %s (no ref codes)." % promo_name)
        return
    print("ref\tpromo\tgroup_jid\tlink")
    for row in ref_map(jids, tracking, promo_name):
        print("\t".join(row))


def _preview(caption, tracking):
    if not tracking:
        return caption
    line = tracking.get("link_line") or DEFAULT_LINK_LINE
    return render_caption(caption, "<REF>", "<ref_link>", line)


def _print_dry_run(jids, weekly, weekday, sent_map, today, image_path, rotating, text,
                   eligible, pending, variants=None, tracking=None, day=None,
                   variant_images=None):
    print("Targets (resolved JIDs): %d" % len(jids))
    variants = variants or [text]
    multi = len(variants) > 1
    for j in jids:
        allowed = weekly.get(j)
        if allowed is not None and weekday not in allowed:
            days = ",".join(_WEEKDAY_ABBR[d] for d in sorted(allowed))
            mark = "skipped today (weekly: %s)" % days
        elif sent_map.get(j) == today:
            mark = "already sent today"
        elif allowed is not None:
            days = ",".join(_WEEKDAY_ABBR[d] for d in sorted(allowed))
            mark = "would send (weekly: %s)" % days
        else:
            mark = "would send"
        extra = ""
        if multi or tracking:
            idx, _, ref = caption_for(j, variants, day=day, allowed=allowed,
                                      tracking=tracking)
            if multi:
                extra += "  variant %d/%d" % (idx + 1, len(variants))
            if ref:
                extra += "  ref %s" % ref
            if variant_images and idx in variant_images:
                extra += "  image %s" % os.path.basename(variant_images[idx])
        print("  - %s  [%s]%s" % (j, mark, extra))
    print(("Image (today): %s" if rotating else "Image: %s") % image_path)
    for k, path in sorted((variant_images or {}).items()):
        print("Image (variant %d/%d): %s" % (k + 1, len(variants), path))
    if multi:
        for k, v in enumerate(variants):
            print("\n----- message (variant %d/%d) -----" % (k + 1, len(variants)))
            print(_preview(v, tracking))
    else:
        print("\n----- message -----")
        print(_preview(variants[0], tracking))
    print("-------------------")
    if weekly:
        print("\n[dry-run] resolved=%d eligible=%d pending=%d"
              % (len(jids), len(eligible), len(pending)))
    else:
        print("\n[dry-run] resolved=%d pending=%d" % (len(jids), len(pending)))


# ----------------------------------------------------------- send spacing ----
DEFAULT_SEND_DELAY_MIN = 20.0
DEFAULT_SEND_DELAY_MAX = 60.0
# Floor for a gap squeezed to fit a short job timeout (see fit_gap_bounds).
MIN_SEND_DELAY = 5.0
# Wall-clock budgeted per send when planning a run. Observed sends take ~7 s;
# the margin covers a slow gateway without being as pessimistic as the 180 s
# per-send subprocess timeout, which only caps a pathological send.
SEND_ALLOWANCE_SECONDS = 12.0
# Share of the cron job's timeout a run may plan to spend, leaving room for
# startup, invite resolution and state writes.
RUN_BUDGET_FRACTION = 0.9


def _seconds(value, label, default):
    """Coerce ``value`` to a non-negative finite float, else warn and return
    ``default``. ``inf`` would sleep until the job timeout kills the run and
    ``nan`` would silently collapse the gap to zero, so both are rejected like a
    negative value rather than clamped."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        sys.stderr.write("WARN: ignoring non-numeric %s=%r\n" % (label, value))
        return default
    if not math.isfinite(seconds):
        sys.stderr.write("WARN: ignoring non-finite %s=%r\n" % (label, value))
        return default
    if seconds < 0:
        sys.stderr.write("WARN: ignoring negative %s=%r\n" % (label, value))
        return default
    return seconds


def _env_seconds(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return _seconds(raw, name, default)


def send_gap_bounds(send_delay=None, send_delay_min=None, send_delay_max=None):
    """(min, max) seconds between consecutive sends. A legacy fixed
    ``send_delay`` pins both bounds to it; otherwise each bound comes from its
    argument, else ``PROMO_SEND_DELAY_MIN`` / ``PROMO_SEND_DELAY_MAX``, else the
    20 / 60 s defaults. A negative or non-finite value is rejected with a warning
    in favour of the default, and bounds are swapped if inverted."""
    if send_delay is not None:
        lo = hi = _seconds(send_delay, "send_delay", DEFAULT_SEND_DELAY_MIN)
        return (lo, hi)
    lo = (_seconds(send_delay_min, "send_delay_min", DEFAULT_SEND_DELAY_MIN)
          if send_delay_min is not None
          else _env_seconds("PROMO_SEND_DELAY_MIN", DEFAULT_SEND_DELAY_MIN))
    hi = (_seconds(send_delay_max, "send_delay_max", DEFAULT_SEND_DELAY_MAX)
          if send_delay_max is not None
          else _env_seconds("PROMO_SEND_DELAY_MAX", DEFAULT_SEND_DELAY_MAX))
    if lo > hi:
        lo, hi = hi, lo
    return (lo, hi)


_PROBE_CRON_TIMEOUT = object()


def cron_timeout_seconds(script=None):
    """Seconds the gateway allows this run, or None when that is unknowable.

    A promo cron's job timeout lives in the gateway's cron state on the volume,
    NOT in this repo: merging a longer ``--timeout-seconds`` into
    deploy/install-promo.sh changes nothing live until the six
    ``deploy/install-*-promo.sh`` installers are re-run, and no deploy step runs
    them. A run that plans for the merged timeout would therefore be killed
    part-way by the stale one, and the groups it had not reached yet would miss
    the day's ad. So ask the gateway what this job's limit actually is and plan
    inside it (see :func:`fit_gap_bounds`).

    Matches the job whose command argv ends with this script's ``<promo>/<file>``
    path. Returns None when the lookup fails or no job matches (e.g. a manual
    run), which leaves the configured gaps untouched.
    ``PROMO_RUN_TIMEOUT_SECONDS`` overrides the lookup; 0 means "no limit"."""
    raw = os.environ.get("PROMO_RUN_TIMEOUT_SECONDS")
    if raw and raw.strip():
        override = _env_seconds("PROMO_RUN_TIMEOUT_SECONDS", None)
        if override is not None:  # a rejected value falls through to the lookup
            return override or None  # 0 -> no limit
    script = os.path.abspath(script or sys.argv[0] or "")
    promo_dir = os.path.basename(os.path.dirname(script))
    if not promo_dir or not os.path.basename(script):
        return None  # nothing specific enough to match a single job against
    tail = os.path.join(promo_dir, os.path.basename(script))
    try:
        p = subprocess.run(["openclaw", "cron", "list", "--all", "--json"],
                           capture_output=True, text=True, timeout=60,
                           env=gateway_env())
    except Exception as ex:
        sys.stderr.write("WARN: could not read this job's cron timeout: %s\n" % ex)
        return None
    if p.returncode != 0:
        sys.stderr.write("WARN: could not read this job's cron timeout: %s\n"
                         % ((p.stderr or p.stdout or "").strip()[:200]))
        return None
    try:
        jobs = (json.loads(p.stdout or "{}") or {}).get("jobs") or []
    except ValueError as ex:
        sys.stderr.write("WARN: could not parse cron list output: %s\n" % ex)
        return None
    for job in jobs:
        payload = (job or {}).get("payload") or {}
        argv = payload.get("argv") or []
        if not any(isinstance(a, str) and a.replace("\\", "/").endswith(tail)
                   for a in argv):
            continue
        raw_timeout = payload.get("timeoutSeconds")
        if raw_timeout is None:
            return None  # job runs unbounded; nothing to fit the run inside
        return _seconds(raw_timeout, "timeoutSeconds", None) or None
    return None


def min_timeout_seconds(count):
    """Shortest job timeout a ``count``-group run can fit in, even once its gaps
    are squeezed to ``MIN_SEND_DELAY``."""
    if count < 1:
        return 0.0
    return (count * SEND_ALLOWANCE_SECONDS
            + max(0, count - 1) * MIN_SEND_DELAY) / RUN_BUDGET_FRACTION


def fit_gap_bounds(lo, hi, count, timeout):
    """Shrink ``(lo, hi)`` so a ``count``-group run plans to finish inside
    ``timeout`` seconds, returning the bounds unchanged when there is room or no
    timeout is known. Both bounds scale by the same factor, so gaps stay random
    instead of collapsing to the fixed cadence this spacing exists to avoid, and
    the upper bound never drops below ``MIN_SEND_DELAY``. Delivering every group
    with tighter random gaps beats being killed part-way with groups unreached.

    A timeout below :func:`min_timeout_seconds` cannot fit the run even at that
    floor. Squeezing further would trade the spacing away and still not finish,
    so the floor stands, the run sends as far as it gets (per-group state keeps a
    re-run from double-posting) and the warning says so instead of promising a
    complete run."""
    if not timeout or count < 2 or hi <= 0:
        return (lo, hi)
    room = timeout * RUN_BUDGET_FRACTION - count * SEND_ALLOWANCE_SECONDS
    allowed = room / (count - 1)
    if allowed >= hi:
        return (lo, hi)
    # Never squeeze below the floor (nor above the configured maximum, for a
    # range deliberately set tighter than the floor): past that point the
    # spacing would be traded away without buying a complete run.
    allowed = max(allowed, min(MIN_SEND_DELAY, hi))
    # Keep the floor on the lower bound too, so a timeout just above the
    # feasible minimum cannot produce sub-floor gaps. A range deliberately
    # configured below the floor keeps its shape instead of being pushed up.
    new_lo = min(max(lo * (allowed / hi), MIN_SEND_DELAY if lo >= MIN_SEND_DELAY
                     else 0.0), allowed)
    if (count - 1) * allowed <= room + 1e-6:
        sys.stderr.write(
            "WARN: this cron job's timeout is %.0f s, too short for %d groups at "
            "%.0f-%.0f s gaps; spacing them %.0f-%.0f s instead so every group "
            "still gets today's ad. Re-run deploy/install-*-promo.sh to apply the "
            "longer timeout and restore the full gaps.\n"
            % (timeout, count, lo, hi, new_lo, allowed))
    else:
        sys.stderr.write(
            "WARN: this cron job's timeout is %.0f s, too short for %d groups "
            "even at the %.0f s gap floor; spacing them %.0f-%.0f s and the run "
            "may still be killed before the last groups (a re-run today resumes "
            "where it stopped). Give the job at least %.0f s "
            "(PROMO_CRON_TIMEOUT_SECONDS) and re-run deploy/install-*-promo.sh.\n"
            % (timeout, count, MIN_SEND_DELAY, new_lo, allowed,
               min_timeout_seconds(count)))
    return (new_lo, allowed)


def broadcast(*, text, image, targets_file, state_file, send_delay=None, argv=None,
              tracking=None, promo_name=None, variant_images=None,
              send_delay_min=None, send_delay_max=None, rng=None, sleep=None,
              cron_timeout=_PROBE_CRON_TIMEOUT):
    """Resolve targets, skip groups already sent today (and weekly-limited groups
    off their configured weekday), then send the advert to each pending group,
    committing state per group so a crash or retry never double-posts. Returns a
    process exit code: 0 on success, 1 if any hard (non-permission) send failed.

    ``text`` is one caption (str, sent as-is) or a list of caption variants that
    rotate per group per post (see :func:`variant_index`). ``tracking`` is
    optional: ``{"prefix": "BH", "number": "+263...", "label": "borehole"}`` plus
    optional ``prefill`` / ``link_line`` templates; when set, each group's caption
    carries its ref code + wa.me link. ``variant_images`` is optional:
    ``{variant_index: image_path}`` pins a fixed image to a caption variant (e.g. a
    themed caption that needs its matching photo); other variants keep ``image``.
    ``--refs`` prints the ref mapping and exits without sending or touching state.

    Consecutive send attempts are spaced by ``rng.uniform(min, max)`` seconds
    (see :func:`send_gap_bounds`; default 20-60 s), tightened if needed to fit
    this cron job's live timeout (see :func:`cron_timeout_seconds` and
    :func:`fit_gap_bounds`) so a run is never killed with groups unreached.
    ``send_delay`` is the legacy fixed gap and, when given, overrides the range.
    ``rng`` (anything with a ``uniform`` method, default the ``random`` module),
    ``sleep`` (default ``time.sleep``) and ``cron_timeout`` (default: ask the
    gateway; None disables the fit) are injectable for tests."""
    args = sys.argv[1:] if argv is None else argv
    show_refs = "--refs" in args
    dry = "--dry-run" in args or show_refs
    day = date.today()
    today = day.isoformat()
    variants = _variants(text)
    if tracking:
        missing = [k for k in ("prefix", "number") if not tracking.get(k)]
        if missing:
            raise ValueError("tracking config missing: %s" % ", ".join(missing))
    if not promo_name:
        promo_name = os.path.basename(os.path.dirname(os.path.abspath(targets_file)))

    image_path, rotating = _resolve_image(image)
    if not image_path or not os.path.exists(image_path):
        sys.stderr.write("ERROR: promo image missing: %s\n" % image_path)
        return 1
    variant_images = dict(variant_images or {})
    for k, path in variant_images.items():
        if not 0 <= k < len(variants):
            raise ValueError("variant_images index out of range: %r" % k)
        if not path or not os.path.exists(path):
            sys.stderr.write("ERROR: promo image missing: %s\n" % path)
            return 1

    entries = load_targets(targets_file)
    weekly = load_weekly_targets(targets_file)
    # Weekly-limited groups are valid send targets too; fold them into the entry
    # list (so they get resolved/deduped), then gate them by weekday below.
    all_entries = entries + [j for j in weekly if j not in entries]
    if not all_entries:
        print("NO_REPLY")  # nothing configured -> stay silent
        return 0

    st = load_state(state_file)
    jids = resolve_targets(all_entries, st, dry=dry)
    if not jids:
        sys.stderr.write("No resolvable group targets.\n")
        print("NO_REPLY")
        return 0

    weekday = date.today().weekday()

    def eligible_today(jid):
        """A weekly-limited group is only eligible on its configured weekdays;
        every other group is eligible every day."""
        allowed = weekly.get(jid)
        return allowed is None or weekday in allowed

    eligible = [j for j in jids if eligible_today(j)]
    sent_map = st["sent"]
    pending = [j for j in eligible if sent_map.get(j) != today]

    if show_refs:
        _print_refs(jids, tracking, promo_name)
        return 0

    if dry:
        _print_dry_run(jids, weekly, weekday, sent_map, today, image_path, rotating,
                       text, eligible, pending, variants=variants, tracking=tracking,
                       day=day, variant_images=variant_images)
        return 0

    if not pending:
        print("NO_REPLY")  # every group already got today's ad
        return 0

    gap_min, gap_max = send_gap_bounds(send_delay, send_delay_min, send_delay_max)
    if cron_timeout is _PROBE_CRON_TIMEOUT:
        # Only worth asking when there is more than one send to space out.
        cron_timeout = cron_timeout_seconds() if gap_max > 0 and len(pending) > 1 else None
    gap_min, gap_max = fit_gap_bounds(gap_min, gap_max, len(pending), cron_timeout)
    rng = rng or random
    sleep = sleep or time.sleep

    failures = 0
    skipped = 0
    sent = 0
    for i, jid in enumerate(pending):
        if i > 0:
            # Random (not fixed) gap between send attempts: a metronome-regular
            # cadence is what WhatsApp spam detection reportedly flags. State is
            # committed per group before this sleep, so a kill mid-gap is safe.
            sleep(rng.uniform(gap_min, gap_max))
        idx, caption, _ = caption_for(jid, variants, day=day, allowed=weekly.get(jid),
                                      tracking=tracking)
        ok, out = send_ad(jid, caption, variant_images.get(idx, image_path))
        if ok:
            sent_map[jid] = today
            save_state(state_file, st)  # commit per group: crash/retry never double-posts
            sent += 1
        elif is_permission_error(out):
            # Bot can't post here (e.g. admins-only group). Expected and
            # permanent: log and skip WITHOUT failing the run. Deliberately not
            # marked sent, so if the bot later becomes an admin it resumes posting
            # automatically. Raw error kept in the log so misclassification shows.
            skipped += 1
            sys.stderr.write("SKIPPED (no permission to post) %s:\n%s\n" % (jid, out))
        else:
            failures += 1
            sys.stderr.write("SEND FAILED for %s:\n%s\n" % (jid, out))

    save_state(state_file, st)  # persist any newly-cached invite resolutions

    print("SENT %d/%d (skipped %d, failures %d)" % (sent, len(pending), skipped, failures))
    return 1 if failures else 0
