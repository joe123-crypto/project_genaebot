"""Shared delivery engine for the deterministic WhatsApp promo crons.

``cron/genaie-promo``, ``cron/building-materials-promo``, and ``cron/cv-promo``
each post a FIXED advert (caption + image) to a set of WhatsApp groups, once per
day per group, through the local gateway, recording a group as "sent
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
Set ``GENAIE_CAMPAIGN_TRANSPORT=rpc`` to use one lazy Node SDK client per campaign;
the image defaults to the transient CLI while net billing savings are unproven.
Set ``GENAIE_AD_ASSETS_SOURCE=r2`` to take the ad images from R2 instead of the
ones baked into the container (see :mod:`ad_assets`).
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
from gateway_campaign import active_client, campaign_transport
import ad_assets
import config_shadow
import operator_config
import promo_config


# ------------------------------------------------------------------ gateway ----
def gateway_env():
    """Env for nested ``openclaw`` calls so they reach the live gateway's WhatsApp
    channel. A bare ``openclaw message send`` runs standalone and reports
    "Channel is unavailable: whatsapp"; it must connect to the running gateway as
    a client, which needs OPENCLAW_GATEWAY_TOKEN set and OPENCLAW_HOME unset."""
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


def account_connectivity(accounts):
    """Read existing listeners, never start a WhatsApp session or probe the phone.

    Missing/malformed status fails closed for the requested accounts. This check
    precedes submission, unlike an uncertain result from an already-issued send.
    """
    result = {account: False for account in accounts}
    try:
        client = active_client()
        if client is not None:
            ok, output = client.request("channels.status", {})
            if not ok:
                return result
            status = json.loads(output)
        else:
            response = subprocess.run(
                ["openclaw", "channels", "status", "--json"],
                env=gateway_env(), capture_output=True, text=True, timeout=30,
                check=True)
            status = json.loads(response.stdout)
        for entry in status["channelAccounts"]["whatsapp"]:
            account = entry.get("accountId")
            if account in result:
                result[account] = entry.get("connected") is True
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            subprocess.SubprocessError):
        sys.stderr.write("WARN: WhatsApp account status unavailable; pending sends deferred.\n")
    return result


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
# Every reader below takes EITHER an already-resolved targets payload (a dict)
# or a path to a targets file. A run resolves its payload once, in
# ``promo_config.resolve``, and passes that dict down: the engine used to open
# the file five times, so a config change landing mid-run could split one run
# across two configs. The path form is what the per-promo CI tests use.
def load_targets(source):
    """Return the list of raw target entries (JIDs and/or invite links)."""
    if isinstance(source, dict):
        data = source
    else:
        try:
            with open(source) as f:
                data = json.load(f)
        except FileNotFoundError:
            sys.stderr.write("ERROR: targets file not found: %s\n" % source)
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


def _targets_doc(source):
    """The targets payload as a dict, or {} when the file is absent, unreadable,
    not JSON or not an object. Shared by the optional-section readers below,
    which all treat a missing section as "nothing configured" rather than an
    error; :func:`load_targets` reports its own failures instead, because a promo
    with no resolvable group list has nothing to do."""
    if isinstance(source, dict):
        return source
    try:
        with open(source) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_weekly_targets(source):
    """Return {jid: frozenset(weekday ints)} for groups posted to only on certain
    weekdays. A JID listed here is posted to ONLY on its weekdays; a JID absent
    from this map (i.e. only in "groups") is posted to every day. Entries with no
    valid weekday, or a bad JID, are ignored."""
    raw = _targets_doc(source).get("weekly_groups") or {}
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


# Account a group belongs to when targets.json pins none. The bot's original
# WhatsApp line is the implicit default; a second line is tagged per group.
DEFAULT_ACCOUNT = "default"


def load_account_targets(source):
    """Return {jid: account_id} for groups whose ad is sent from a non-default
    WhatsApp account. A JID absent from the map (i.e. only in "groups") is sent
    from the default account. The map lets the promo group-load be spread across
    two WhatsApp numbers so no single account is a member of a suspiciously large
    number of groups."""
    raw = _targets_doc(source).get("accounts") or {}
    out = {}
    if isinstance(raw, dict):
        for jid, account in raw.items():
            jid = str(jid).strip()
            account = str(account).strip()
            if jid.endswith("@g.us") and account:
                out[jid] = account
    return out


def load_account_numbers(source):
    """Return {account_id: advertised_number} so the engine can render each
    group's caption + wa.me link with the selected contact account
    (or the posting account when no contact is selected). Comes from membership.json's
    defaults.accounts, carried into every targets.json by make-promo-targets."""
    raw = _targets_doc(source).get("account_numbers") or {}
    out = {}
    if isinstance(raw, dict):
        for account, number in raw.items():
            account = str(account).strip()
            number = str(number).strip()
            if account and number:
                out[account] = number
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
    OpenClaw WhatsApp directory.
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
    # Under the state file's lock, so an operator clearing a deleted group's
    # records cannot read this file between two sends and write back a copy
    # without the receipt committed in between -- which would let the next run
    # post the same advert to that group again. Locking fails open: a lock that
    # cannot be taken never stops a send.
    json_store.locked_write_json_atomic(state_file, st)


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


# A send can also be rejected because the WhatsApp Web link is momentarily down:
# the gateway drops and reconnects every ~30-50 min (``Web connection closed``
# status 428/500/503, auto-retry in ~2 s) and a send landing in that gap returns
# these markers. The rejection is NOT final, though: the gateway keeps the
# request in its outbound delivery queue and replays it as soon as the listener
# is back. Live logs show it both times this happened -- 2026-10-18 06:24:02 UTC
# (error, then the same group's ad "Sent" 1.2 s later with no new CLI request)
# and 2026-10-19 15:17:07 UTC (OnPoint ZW Ads: error, gateway replay "Sent" 0.4 s
# later, then our own 20 s retry posted the SAME ad a second time). So re-sending
# here double-posts. Treat it as handed to the gateway: record the group as sent
# for today and move on, never re-send. A double post in someone else's group is
# exactly the spammy pattern that gets the number reported, whereas the rare
# replay that never lands costs at most one ad for one day.
# Deliberately NOT matched (still hard failures): ``unknown_after_send`` and a
# subprocess timeout, which are ambiguous in a different way.
NOT_DISPATCHED_MARKERS = (
    "no active whatsapp web listener",
    "platformmessagenotdispatchederror",
    "channel is unavailable",
    "not dispatched",
)


def is_not_dispatched_error(out):
    low = (out or "").casefold()
    return any(marker in low for marker in NOT_DISPATCHED_MARKERS)


def send_ad(target, text, image_path, account=None):
    """Send the ad (caption + image) to one group. Returns (ok, combined_output).
    ``account`` names the WhatsApp line it is sent from. A non-default account is
    passed explicitly; the default one is deliberately NOT, because the gateway
    rejects an account id it does not have in its registry ("Unknown account
    ...") and ``channels.whatsapp.accounts`` may legitimately be absent -- a
    single-account gateway has only the implicit default. Omitting it there keeps
    sends working either way. What stops the gateway picking the wrong line once a
    second account exists is ``channels.whatsapp.defaultAccount: "default"`` in
    config/openclaw.template.json: without it the default is "default" if present
    else the first account id sorted, and "ads" sorts before "default"
    (deploy/test_promo_captions_tracking.py pins that config contract)."""
    client = active_client()
    if client is not None:
        return client.send(target, text, image_path, account=account)
    cmd = ["openclaw", "message", "send", "--channel", "whatsapp",
           "--target", target, "--message", text,
           "--media", image_path, "--json"]
    if account and account != DEFAULT_ACCOUNT:
        cmd += ["--account", account]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           env=gateway_env())
    except Exception as ex:
        return (False, "exec error: %s" % ex)
    out = (p.stdout or "") + (p.stderr or "")
    ok = (p.returncode == 0) and ('"messageId"' in out) and ('"error"' not in (p.stdout or ""))
    return (ok, out)


# --------------------------------------------------------------------- run ----
def _resolve_image(image, day=None):
    """``image`` is either a single path (fixed) or a list of paths that rotate
    daily by date ordinal so a group never sees the same image two days running.
    Returns (path_or_None, rotating)."""
    if isinstance(image, (list, tuple)):
        assets = [a for a in image if a]
        if not assets:
            return (None, True)
        return (assets[(day or date.today()).toordinal() % len(assets)], True)
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


def tracking_for(tracking, jid, bot_number=None):
    """(ref, link) for ``jid`` under a tracking config, or (None, None). The
    wa.me link points at ``bot_number`` (the account that posts to ``jid``) when
    given, else the tracking config's own pinned ``number`` (e.g. appliance-repairs
    routes to a fixed shop line). No number at all ⇒ (ref, None)."""
    if not tracking:
        return (None, None)
    ref = ref_code(tracking["prefix"], jid)
    number = bot_number or tracking.get("number")
    if not number:
        return (ref, None)
    prefill = (tracking.get("prefill") or DEFAULT_PREFILL).replace(
        "{label}", tracking.get("label", "")).replace("{ref}", ref)
    return (ref, ref_link(number, prefill))


def render_caption(caption, ref=None, link=None, link_line=DEFAULT_LINK_LINE,
                   bot_number=None):
    """Substitute ``{ref_link}`` / ``{ref}`` / ``{bot_number}`` in ``caption``.
    Uses plain ``str.replace`` (not ``str.format``), so any other braces in
    operator copy are left alone. ``{bot_number}`` renders the posting account's
    own advertised number, so each WhatsApp line advertises itself. With tracking
    on and no ``{ref_link}`` placeholder in the caption, ``link_line`` is appended
    after a blank line. With nothing to substitute the caption is returned
    unchanged."""
    if not ref and not link and not bot_number:
        return caption
    had_link = "{ref_link}" in caption
    out = (caption.replace("{ref_link}", link or "")
                  .replace("{ref}", ref or "")
                  .replace("{bot_number}", bot_number or ""))
    if link and not had_link and link_line:
        line = link_line.replace("{ref_link}", link).replace("{ref}", ref or "")
        out = out.rstrip("\n") + "\n\n" + line
    return out


def caption_for(jid, variants, day=None, allowed=None, tracking=None,
                bot_number=None):
    """(variant_index, rendered caption, ref) for one group on ``day``.
    ``bot_number`` is the advertised number of the account that posts to ``jid``."""
    idx = variant_index(jid, len(variants), day=day, allowed=allowed)
    ref, link = tracking_for(tracking, jid, bot_number=bot_number)
    line = (tracking or {}).get("link_line") or DEFAULT_LINK_LINE
    return (idx, render_caption(variants[idx], ref, link, line,
                                bot_number=bot_number), ref)


def ref_map(jids, tracking, promo_name, number_for=None):
    """[(ref, promo_name, jid, link)] for decoding ref codes later. ``number_for``
    is an optional ``jid -> advertised number`` lookup so links reflect the
    posting account."""
    if not tracking:
        return []
    number_of = number_for or (lambda _jid: None)
    return [(ref, promo_name, j, link)
            for j in jids
            for ref, link in [tracking_for(tracking, j, bot_number=number_of(j))]]


def _print_refs(jids, tracking, promo_name, number_for=None):
    if not tracking:
        print("Tracking is not enabled for %s (no ref codes)." % promo_name)
        return
    print("ref\tpromo\tgroup_jid\tlink")
    for row in ref_map(jids, tracking, promo_name, number_for=number_for):
        print("\t".join(row))


def _preview(caption, tracking):
    if not tracking:
        return caption
    line = tracking.get("link_line") or DEFAULT_LINK_LINE
    return render_caption(caption, "<REF>", "<ref_link>", line,
                          bot_number="<bot_number>")


def _print_dry_run(jids, weekly, weekday, sent_map, today, image_path, rotating, text,
                   eligible, pending, variants=None, tracking=None, day=None,
                   variant_images=None, cap_decisions=None, promo=None,
                   account_for=None, number_for=None):
    print("Targets (resolved JIDs): %d" % len(jids))
    variants = variants or [text]
    multi = len(variants) > 1
    cap_decisions = cap_decisions or {}
    eligible_set = set(eligible)
    for j in jids:
        allowed = weekly.get(j)
        if allowed is not None and weekday not in allowed:
            days = ",".join(_WEEKDAY_ABBR[d] for d in sorted(allowed))
            mark = "skipped today (weekly: %s)" % days
        elif cap_decisions and j not in eligible_set:
            info = cap_decisions.get(j) or {}
            mark = "skipped today (at daily cap; today goes to %s)" % (
                ", ".join(info.get("allowed") or []) or "nothing")
        elif sent_map.get(j) == today:
            mark = "already sent today"
        elif allowed is not None:
            days = ",".join(_WEEKDAY_ABBR[d] for d in sorted(allowed))
            mark = "would send (weekly: %s)" % days
        else:
            mark = "would send"
        extra = ""
        info = cap_decisions.get(j)
        if info and info.get("known"):
            extra += "  cap %s/%d (tier %s, replies %.1f)" % (
                info.get("cap"), len(info.get("promos") or []), info.get("tier"),
                info.get("score") or 0.0)
        account = account_for(j) if account_for else DEFAULT_ACCOUNT
        number = number_for(j) if number_for else None
        if account and account != DEFAULT_ACCOUNT:
            extra += "  account %s" % account
        if number:
            extra += "  contact %s" % number
        if multi or tracking:
            idx, _, ref = caption_for(j, variants, day=day, allowed=allowed,
                                      tracking=tracking, bot_number=number)
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
    capped = sum(1 for j in jids if cap_decisions and j not in eligible_set
                 and (weekly.get(j) is None or weekday in weekly[j]))
    suffix = " capped=%d" % capped if cap_decisions else ""
    if weekly:
        print("\n[dry-run] resolved=%d eligible=%d pending=%d%s"
              % (len(jids), len(eligible), len(pending), suffix))
    else:
        print("\n[dry-run] resolved=%d pending=%d%s"
              % (len(jids), len(pending), suffix))
    if cap_decisions:
        print("[dry-run] per-group caps are ON for %s (membership.json "
              "caps_enabled)" % (promo or "this promo"))


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
# Upper bound on the random delay taken before a run's FIRST send, on top of the
# random gaps between sends. Each promo cron fires at a fixed minute, so without
# this the account's day-to-day pattern stays perfectly periodic even though the
# gaps inside a run are not. The delay is charged against the job's timeout
# budget (see fit_gap_bounds callers), never taken on --dry-run/--refs, and never
# taken when there is nothing to send.
DEFAULT_START_JITTER_SECONDS = 900.0
# Most of a job's timeout the start jitter may spend. A stale 600 s timeout with
# a full 900 s head start would otherwise kill the run before its first send.
START_JITTER_BUDGET_FRACTION = 0.25


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


# A routine WhatsApp Web reconnect (status 500/428/503) relinks within a few
# seconds; give a dropped listener this long to return before deferring the rest
# of its groups. PROMO_RECONNECT_GRACE_SECONDS=0 restores the immediate deferral.
DEFAULT_RECONNECT_GRACE = 90.0
RECONNECT_POLL_SECONDS = 5.0


def wait_for_account(check_accounts, account, sleep=None, grace=None,
                     poll=RECONNECT_POLL_SECONDS):
    """True once ``account`` reports connected, polling every ``poll`` seconds
    for up to ``grace`` seconds (default ``PROMO_RECONNECT_GRACE_SECONDS``, else
    90 s). The grace counts wall-clock time, including slow status reads; a
    read already in flight at the deadline is allowed to finish. Reads listener
    status only; never starts a session or sends."""
    sleep = sleep or time.sleep
    if grace is None:
        grace = _env_seconds("PROMO_RECONNECT_GRACE_SECONDS", DEFAULT_RECONNECT_GRACE)
    # Bounded by both the sleeps taken (deterministic under an injected sleep)
    # and wall-clock time, so slow status reads cannot stretch the grace.
    deadline = time.monotonic() + grace
    waited = 0.0
    while True:
        if check_accounts({account}).get(account) is True:
            return True
        if waited >= grace or time.monotonic() >= deadline:
            return False
        step = min(poll, grace - waited)
        sleep(step)
        waited += step


def reconnect_grace(run_deadline, remaining, gap_max):
    """Seconds :func:`wait_for_account` may wait mid-run. The configured grace
    (``PROMO_RECONNECT_GRACE_SECONDS``, else 90 s), shortened so the groups still
    to send -- at the planned worst case of ``SEND_ALLOWANCE_SECONDS`` plus the
    longest gap each -- fit before the cron job's timeout at ``run_deadline``
    (a ``time.monotonic()`` value; ``None`` when no timeout is known). Without
    this cap a 90 s wait, repeated on every drop of a flapping line, could get
    the run killed before groups on healthy lines were reached."""
    grace = _env_seconds("PROMO_RECONNECT_GRACE_SECONDS", DEFAULT_RECONNECT_GRACE)
    if run_deadline is None:
        return grace
    needed = len(remaining) * (SEND_ALLOWANCE_SECONDS + gap_max)
    room = max(0.0, run_deadline - time.monotonic() - needed)
    if room < grace:
        sys.stderr.write(
            "WARN: reconnect grace cut from %.0f s to %.0f s so the %d groups "
            "still to send fit inside this cron job's timeout.\n"
            % (grace, room, len(remaining)))
        return room
    return grace


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


def start_jitter_bound(start_jitter=None):
    """Upper bound (seconds) for the pre-first-send delay. ``start_jitter``
    overrides the ``PROMO_START_JITTER_SECONDS`` env var; ``0`` disables it."""
    if start_jitter is not None:
        return _seconds(start_jitter, "start_jitter", DEFAULT_START_JITTER_SECONDS)
    return _seconds(os.environ.get("PROMO_START_JITTER_SECONDS",
                                   DEFAULT_START_JITTER_SECONDS),
                    "PROMO_START_JITTER_SECONDS", DEFAULT_START_JITTER_SECONDS)


def cron_timeout_seconds(script=None):
    """Seconds the gateway allows this run, or None when that is unknowable.

    A promo cron's job timeout lives in the gateway's cron state on the volume,
    NOT in this repo: merging a longer ``--timeout-seconds`` into
    deploy/install-promo.sh changes nothing live until the ten
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


# ------------------------------------------------------------- group caps ----
# Sentinel: load membership.json and obey its caps_enabled setting.
_PROBE_CAPS = object()
_PROBE_MEMBERSHIP = object()
PROMO_DIR_SUFFIX = "-promo"


def promo_key(promo_name):
    """``"cv-promo"`` -> ``"cv"``: the key membership.json files a promo under."""
    name = str(promo_name or "")
    if name.endswith(PROMO_DIR_SUFFIX):
        return name[:-len(PROMO_DIR_SUFFIX)]
    return name


def apply_caps(promo_name, jids, day, caps=_PROBE_CAPS, membership_path=None,
               record=True, membership=_PROBE_MEMBERSHIP, switches=None):
    """Drop the groups whose daily ad allowance is spoken for by other campaigns
    today (see :mod:`promo_caps`). Returns ``(kept, decisions)``, with
    ``decisions`` empty when caps are off.

    ``membership`` and ``switches`` come from the run's single
    ``promo_config.resolve``, so the caps read the same config the targeting did
    rather than re-reading the file (or Firestore) for themselves. Explicit
    ``membership=None`` means resolution found no usable membership. Without them
    the membership file and the cron manifest are read, as before.

    Deliberately fail-open: a missing membership file, bad data or an import
    problem leaves every group in place and logs a warning. A capacity nicety
    must never be the reason a day's ads do not go out."""
    if caps is None or not jids:
        return list(jids), {}
    try:
        import promo_membership

        m = (promo_membership.load(membership_path)
             if membership is _PROBE_MEMBERSHIP else membership)
        if m is None:
            return list(jids), {}
        if caps is _PROBE_CAPS:
            if not promo_membership.caps_enabled(m):
                return list(jids), {}
            import promo_caps as caps_mod
        else:
            caps_mod = caps
        disabled = None
        if switches is not None:
            disabled = [slug for slug, on in switches.items() if not on]
        kept, decisions = caps_mod.filter_for_promo(
            m, promo_key(promo_name), list(jids), day=day, record=record,
            disabled=disabled)
    except Exception as ex:  # fail-open, loudly
        sys.stderr.write("WARN: per-group caps not applied (%s: %s); sending to "
                         "every eligible group.\n" % (type(ex).__name__, ex))
        return list(jids), {}

    dropped = [j for j in jids if j not in set(kept)]
    if dropped:
        sys.stderr.write(
            "CAPS: %d of %d groups are at their daily ad cap and skip %s today.\n"
            % (len(dropped), len(jids), promo_key(promo_name)))
        for jid in dropped:
            info = decisions.get(jid) or {}
            sys.stderr.write(
                "  - %s cap %s (tier %s, score %.1f) goes to: %s\n"
                % (jid, info.get("cap"), info.get("tier"), info.get("score") or 0.0,
                   ", ".join(info.get("allowed") or []) or "nothing"))
    return kept, decisions


@campaign_transport
@config_shadow.alongside
@ad_assets.alongside
def broadcast(*, text, image, targets_file, state_file, send_delay=None, argv=None,
              tracking=None, promo_name=None, variant_images=None,
              send_delay_min=None, send_delay_max=None, rng=None, sleep=None,
              cron_timeout=_PROBE_CRON_TIMEOUT, start_jitter=None,
              caps=_PROBE_CAPS, membership_path=None, account_status=None,
              resolve_config=None, run_day=None, cap_day=None):
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
    A run also waits ``rng.uniform(0, start_jitter_bound())`` before its FIRST
    send (default up to 900 s, ``PROMO_START_JITTER_SECONDS``), charged against
    the timeout budget, so a cron pinned to one minute still starts at a
    different time each day.

    Groups whose daily ad cap is already spoken for by other campaigns are
    dropped before sending (see :mod:`promo_caps`); this is inert until
    membership.json sets ``caps_enabled``. ``caps=None`` disables it outright and
    ``caps=<module>`` injects an alternative, both for tests.

    Where the targeting comes from is :func:`promo_config.resolve`'s decision --
    the generated targets file, or Firestore. It is resolved once per run, and
    ``resolve_config`` injects an alternative resolver for tests. Where the
    PICTURES come from is :func:`ad_assets.resolve`'s decision -- the assets
    baked into this image, or the ones indexed in Firestore with their bytes in
    R2 -- and it hands back local paths either way.

    ``rng`` (anything with a ``uniform`` method, default the ``random`` module),
    ``sleep`` (default ``time.sleep``), ``cron_timeout`` (default: ask the
    gateway; None disables the fit) and ``membership_path`` are injectable for
    tests."""
    args = sys.argv[1:] if argv is None else argv
    show_refs = "--refs" in args
    dry = "--dry-run" in args or show_refs
    day = run_day or date.today()
    today = day.isoformat()
    variants = _variants(text)
    if tracking and not tracking.get("prefix"):
        raise ValueError("tracking config missing: prefix")
    if not promo_name:
        promo_name = os.path.basename(os.path.dirname(os.path.abspath(targets_file)))

    # Which pictures this campaign actually has: the ones baked into the image,
    # or the ones the operator indexed in Firestore and R2 holds the bytes for
    # (see :mod:`ad_assets`). Either way what comes back is local paths, so the
    # rotation below, the existence check and ``send_ad`` are unchanged -- and
    # that existence check is what turns "no asset could be fetched" into a
    # failed run with nothing sent, before a single group is posted to.
    image = ad_assets.resolve(promo_name, image)
    image_path, rotating = _resolve_image(image, day)
    if not image_path or not os.path.exists(image_path):
        sys.stderr.write("ERROR: promo image missing: %s\n" % image_path)
        return 1
    variant_images = ad_assets.resolve(promo_name, dict(variant_images or {}))
    for k, path in variant_images.items():
        if not 0 <= k < len(variants):
            raise ValueError("variant_images index out of range: %r" % k)
        if not path or not os.path.exists(path):
            sys.stderr.write("ERROR: promo image missing: %s\n" % path)
            return 1

    # Where this run's targeting comes from -- the generated targets file, or
    # Firestore. Resolved ONCE: every reader below works off this one payload, so
    # a config change landing mid-run cannot split the run across two configs.
    try:
        source = (resolve_config or promo_config.resolve)(
            promo_name, targets_file=targets_file, membership_path=membership_path)
    except operator_config.ConfigUnavailable as ex:
        # Firestore is canonical and unreadable, with no usable snapshot. There is
        # no safe targeting to fall back to -- the repo's membership.json would be
        # stale, which is how a paused group gets advertised again. Send nothing,
        # and exit non-zero so the cron's failure alert fires.
        sys.stderr.write("ERROR: no operator config to act on: %s\n" % ex)
        print("NO_REPLY")
        return 1

    # A campaign the operator has switched off still has its cron registered on
    # the gateway volume, so the run itself has to stand down. Only in firestore
    # mode: there the switch is the live pause control. A hand-run or --dry-run of
    # a paused campaign still previews, matching promo_caps, which never treats
    # the promo being run as disabled.
    if source["mode"] == promo_config.FIRESTORE and source["enabled"] is False:
        if dry:
            sys.stderr.write("NOTE: %s is switched off; previewing anyway.\n" % promo_name)
        else:
            sys.stderr.write("%s is switched off in the operator config; "
                             "sending nothing.\n" % promo_name)
            print("NO_REPLY")
            return 0

    variants = operator_config.campaign_captions(source, promo_config.promo_key(promo_name), variants)
    variant_images = {index: path for index, path in variant_images.items() if index < len(variants)}

    targets = source["doc"]
    entries = load_targets(targets)
    weekly = load_weekly_targets(targets)
    account_map = load_account_targets(targets)       # {jid: account}
    account_numbers = load_account_numbers(targets)   # {account: number}
    contact_account = targets.get("contact_account")

    # Contact routing is independent of the group's posting account. Explicit
    # third-party tracking destinations retain precedence.
    def account_for(jid):
        return account_map.get(jid, DEFAULT_ACCOUNT)

    def number_for(jid):
        # A promo that pins its own contact number (e.g. appliance-repairs routes
        # to a fixed shop line) always advertises it, whatever account posts the
        # ad. Otherwise use the shared contact account, if configured.
        pinned = (tracking or {}).get("number")
        if pinned:
            return pinned
        return account_numbers.get(contact_account or account_for(jid))

    # Whether this promo's copy/link needs a bot number: it uses {bot_number}, or
    # it tracks without pinning its own number (so the link target comes from the
    # posting account).
    bot_number_used = (any("{bot_number}" in v for v in variants)
                       or (bool(tracking) and not (tracking or {}).get("number")))
    # Weekly-limited groups are valid send targets too; fold them into the entry
    # list (so they get resolved/deduped), then gate them by weekday below.
    all_entries = entries + [j for j in weekly if j not in entries]
    if not all_entries:
        print("NO_REPLY")  # nothing configured -> stay silent
        return 0
    if contact_account is not None and (
            not isinstance(contact_account, str) or contact_account not in account_numbers):
        raise ValueError("contact_account must name an account with a number "
                         "(%s): check defaults.accounts, and regenerate "
                         "targets.json with make-promo-targets when the source "
                         "is the file" % source["source"])
    # Checked only once there is something to send, so a missing/unreadable
    # targets file still exits quietly above (load_targets reports it) instead of
    # being misreported here as a stale, un-regenerated one.
    if bot_number_used and not account_numbers:
        raise ValueError(
            "this promo advertises a per-account number but the targets payload "
            "(%s) has no account_numbers map; check defaults.accounts, and "
            "regenerate targets.json with make-promo-targets when the source is "
            "the file" % source["source"])

    st = load_state(state_file)
    jids = resolve_targets(all_entries, st, dry=dry)
    if not jids:
        sys.stderr.write("No resolvable group targets.\n")
        print("NO_REPLY")
        return 0

    weekday = day.weekday()

    def eligible_today(jid):
        """A weekly-limited group is only eligible on its configured weekdays;
        every other group is eligible every day."""
        allowed = weekly.get(jid)
        return allowed is None or weekday in allowed

    eligible = [j for j in jids if eligible_today(j)]
    # Per-group daily cap: a group in six campaigns does not have to take six
    # ads a day. Rotation decides which campaigns use today's allowance, so this
    # promo may be asked to stand down for a group it still targets.
    eligible, cap_decisions = apply_caps(promo_name, eligible, cap_day or day, caps=caps,
                                         membership_path=membership_path,
                                         record=not dry,
                                         membership=source["membership"],
                                         switches=source["switches"])
    sent_map = st["sent"]
    pending = [j for j in eligible if sent_map.get(j) != today]

    if show_refs:
        _print_refs(jids, tracking, promo_name, number_for=number_for)
        return 0

    if dry:
        _print_dry_run(jids, weekly, weekday, sent_map, today, image_path, rotating,
                       text, eligible, pending, variants=variants, tracking=tracking,
                       day=day, variant_images=variant_images,
                       cap_decisions=cap_decisions, promo=promo_key(promo_name),
                       account_for=account_for, number_for=number_for)
        return 0

    if not pending:
        print("NO_REPLY")  # every group already got today's ad
        return 0

    rng = rng or random
    sleep = sleep or time.sleep
    jitter_bound = start_jitter_bound(start_jitter)

    gap_min, gap_max = send_gap_bounds(send_delay, send_delay_min, send_delay_max)
    if cron_timeout is _PROBE_CRON_TIMEOUT:
        # Worth asking whenever this run plans to wait at all: to space sends,
        # or before the first one.
        wants_timeout = (gap_max > 0 and len(pending) > 1) or jitter_bound > 0
        cron_timeout = cron_timeout_seconds() if wants_timeout else None
    if cron_timeout and jitter_bound > 0:
        # Never wait away the whole job: against a stale (short) timeout a full
        # 15-minute head start could burn the run before its first send. Shrink
        # the RANGE rather than clamping the draw: min(uniform(0, 900), 150)
        # returns exactly 150 s five times in six, so under the live 600 s
        # timeout every promo's first send landed at the same second past the
        # hour -- the fixed daily pattern this wait exists to break.
        jitter_bound = min(jitter_bound, cron_timeout * START_JITTER_BUDGET_FRACTION)
    # A bound of 0 draws nothing at all, so disabling the jitter leaves the rng
    # untouched.
    jitter = rng.uniform(0, jitter_bound) if jitter_bound > 0 else 0.0
    # The wait is charged against the timeout budget, so the gaps are planned
    # with what is left -- otherwise the head start silently eats the margin
    # fit_gap_bounds exists to protect.
    budget = max(0.0, cron_timeout - jitter) if cron_timeout else cron_timeout
    gap_min, gap_max = fit_gap_bounds(gap_min, gap_max, len(pending), budget)

    if jitter > 0:
        print("Waiting %.0f s before the first send (start jitter)." % jitter)
        sleep(jitter)
    # When cron will kill this job: a reconnect wait mid-run must leave room for
    # the groups still to come (see reconnect_grace below).
    run_deadline = time.monotonic() + budget if budget else None

    # Take one fresh snapshot after jitter, immediately before submission. A
    # known-offline target never enters the gateway queue and gets no receipt.
    check_accounts = account_status or account_connectivity
    accounts = {account_for(jid) for jid in pending}
    connected = check_accounts(accounts)
    unavailable = {account for account in accounts if connected.get(account) is not True}
    failures = 0
    skipped = 0
    deferred = 0
    queued = 0
    sent = 0
    attempted = 0
    for position, jid in enumerate(pending):
        account = account_for(jid)
        if account in unavailable:
            deferred += 1
            continue
        if attempted > 0:
            # Random (not fixed) gap between send attempts: a metronome-regular
            # cadence is what WhatsApp spam detection reportedly flags. State is
            # committed per group before this sleep, so a kill mid-gap is safe.
            sleep(rng.uniform(gap_min, gap_max))
        number = number_for(jid)
        if bot_number_used and not number:
            # Can't render this group's own advertised number: skip rather than
            # post an ad with an empty number / broken link. Counts as a failure
            # so the run exits non-zero and the gap is noticed.
            failures += 1
            sys.stderr.write(
                "SEND FAILED for %s: no advertised number for account %r "
                "(check membership.json defaults.accounts)\n" % (jid, account))
            continue
        idx, caption, _ = caption_for(jid, variants, day=day, allowed=weekly.get(jid),
                                      tracking=tracking, bot_number=number)
        ok, out = send_ad(jid, caption, variant_images.get(idx, image_path),
                          account=account)
        attempted += 1
        if (not ok) and (not is_permission_error(out)) and is_not_dispatched_error(out):
            # Link was mid-reconnect: the gateway queued this send and replays it
            # on reconnect (see NOT_DISPATCHED_MARKERS). Re-sending would post the
            # ad twice, so mark it sent and do not retry. Receipted before the
            # recovery wait below, so a kill during that wait cannot re-send it.
            sent_map[jid] = today
            save_state(state_file, st)
            queued += 1
            sys.stderr.write(
                "QUEUED (link mid-reconnect; gateway replays it, not re-sent) %s:\n%s\n"
                % (jid, out))
        if not ok and is_not_dispatched_error(out):
            # Do not submit more work to a listener now known to be offline --
            # unless it comes back within a short grace. WhatsApp drops the Web
            # socket with a 500 every hour or so and relinks in seconds; on
            # 2026-11-05 a send that landed in that window deferred the remaining
            # 15 default-line groups of the borehole run for the whole day.
            grace = reconnect_grace(run_deadline, [
                j for j in pending[position + 1:] if account_for(j) not in unavailable],
                gap_max)
            if not wait_for_account(check_accounts, account, sleep, grace=grace):
                unavailable.add(account)
            if not is_permission_error(out):
                continue
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

    if deferred:
        sys.stderr.write("DEFERRED %d targets: WhatsApp accounts unavailable (%s); "
                         "not submitted or marked sent. No automatic catch-up.\n"
                         % (deferred, ", ".join(sorted(unavailable))))
    print("SENT %d/%d (queued %d, skipped %d, failures %d, deferred %d)"
          % (sent, len(pending), queued, skipped, failures, deferred))
    return 1 if failures else 0
