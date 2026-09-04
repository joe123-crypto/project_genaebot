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

Std-lib only (plus the sibling :mod:`json_store` helper), so it stays importable
from the OpenClaw ``--command`` cron scripts, which are std-lib only by contract.
"""

import os
import sys
import json
import time
import subprocess
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


def _print_dry_run(jids, weekly, weekday, sent_map, today, image_path, rotating, text,
                   eligible, pending):
    print("Targets (resolved JIDs): %d" % len(jids))
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
        print("  - %s  [%s]" % (j, mark))
    print(("Image (today): %s" if rotating else "Image: %s") % image_path)
    print("\n----- message -----")
    print(text)
    print("-------------------")
    if weekly:
        print("\n[dry-run] resolved=%d eligible=%d pending=%d"
              % (len(jids), len(eligible), len(pending)))
    else:
        print("\n[dry-run] resolved=%d pending=%d" % (len(jids), len(pending)))


def broadcast(*, text, image, targets_file, state_file, send_delay=3.0, argv=None):
    """Resolve targets, skip groups already sent today (and weekly-limited groups
    off their configured weekday), then send the advert to each pending group,
    committing state per group so a crash or retry never double-posts. Returns a
    process exit code: 0 on success, 1 if any hard (non-permission) send failed."""
    args = sys.argv[1:] if argv is None else argv
    dry = "--dry-run" in args
    today = date.today().isoformat()

    image_path, rotating = _resolve_image(image)
    if not image_path or not os.path.exists(image_path):
        sys.stderr.write("ERROR: promo image missing: %s\n" % image_path)
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

    if dry:
        _print_dry_run(jids, weekly, weekday, sent_map, today, image_path, rotating,
                       text, eligible, pending)
        return 0

    if not pending:
        print("NO_REPLY")  # every group already got today's ad
        return 0

    failures = 0
    skipped = 0
    sent = 0
    for i, jid in enumerate(pending):
        ok, out = send_ad(jid, text, image_path)
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
        if i < len(pending) - 1:
            time.sleep(send_delay)

    save_state(state_file, st)  # persist any newly-cached invite resolutions

    print("SENT %d/%d (skipped %d, failures %d)" % (sent, len(pending), skipped, failures))
    return 1 if failures else 0
