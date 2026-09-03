#!/usr/bin/env python3
"""
CV-business daily ad poster.

Posts a FIXED, predefined advert (caption + image) for the Genaie CV writing /
revamping service to a configured set of WhatsApp groups, once per day per group.
There is NO AI/LLM generation involved: the post text is a hardcoded constant and
the image is a bundled asset. This script is meant to run from an OpenClaw
`--command` cron (deterministic, no agent turn).

This script OWNS delivery: it sends each WhatsApp message itself via
`openclaw message send --media` and only records a group as "sent today" after a
confirmed send, so delivery and progress are transactional per group (same pattern
as cron/algeria-exhibitions-listener/check_new_events.py).

Targets come from targets.json (JIDs and/or invite links). Invite links are
resolved to group JIDs via `openclaw channels resolve`; the bot must already be a
MEMBER of each group for resolution and sending to work. Resolved JIDs are cached
in the state file to avoid re-resolving every day.

Most groups (the flat "groups" list) get the ad every day. A group may instead be
given a reduced cadence by listing it under "weekly_groups" in targets.json, which
maps a group JID to the weekdays it should be posted to (e.g. Vacancy Lamp twice a
week). The cron still runs daily; a weekly-limited group is simply skipped on days
outside its configured weekdays, so no second cron is needed.

Usage:
  post_cv_ad.py            normal run (resolve, skip groups already sent today, send)
  post_cv_ad.py --dry-run  show resolved targets + the message; no send, no state
                           mutation

Std-lib only.
"""

import os
import sys
import json
import time
import subprocess
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS_FILE = os.environ.get(
    "CV_PROMO_TARGETS", os.path.join(HERE, "targets.json")
)
IMAGE_PATH = os.environ.get(
    "CV_PROMO_IMAGE", os.path.join(HERE, "assets", "cv-ad.png")
)
STATE_FILE = os.environ.get(
    "CV_PROMO_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/cv-promo/state.json",
)
SEND_DELAY_SECONDS = float(os.environ.get("CV_PROMO_SEND_DELAY", "3"))


def openclaw_env():
    """Env for nested `openclaw` calls so they reach the live gateway's WhatsApp
    channel. A bare `openclaw message send` runs standalone and reports
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

# Fixed advert. WhatsApp bold uses *single* asterisks, so the closing line is
# wrapped in single asterisks to render bold (not literal ** ). Everything else is
# verbatim.
POST_TEXT = (
    "Looking for a job but your CV doesn't look professional enough?\n"
    "\n"
    "We help job seekers create and revamp CVs that clearly present their "
    "skills, experience, and achievements.\n"
    "\n"
    "\U0001F4C4 CV Writing\n"
    "\U0001F504 CV Revamping\n"
    "\U0001F4BC Professional presentation\n"
    "\n"
    "\U0001F4F2 +213563719936\n"
    "\U0001F310 genaie.site\n"
    "\n"
    "*Your next opportunity starts with a better CV.*"
)


# ------------------------------------------------------------------ targets ----
def load_targets():
    """Return the list of raw target entries (JIDs and/or invite links)."""
    try:
        with open(TARGETS_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.stderr.write("ERROR: targets file not found: %s\n" % TARGETS_FILE)
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


def load_weekly_targets():
    """Return {jid: frozenset(weekday ints)} for groups posted to only on certain
    weekdays. A JID listed here is posted to ONLY on its weekdays; a JID absent
    from this map (i.e. only in "groups") is posted to every day. Entries with no
    valid weekday, or a bad JID, are ignored."""
    try:
        with open(TARGETS_FILE) as f:
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
                           env=openclaw_env())
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
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"sent": {}, "resolved": {}}
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (json.JSONDecodeError, OSError):
        st = {}
    st.setdefault("sent", {})
    st.setdefault("resolved", {})
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_FILE)


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


def send_ad(target):
    """Send the ad (caption + image) to one group. Returns (ok, combined_output)."""
    cmd = ["openclaw", "message", "send", "--channel", "whatsapp",
           "--target", target, "--message", POST_TEXT,
           "--media", IMAGE_PATH, "--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           env=openclaw_env())
    except Exception as ex:
        return (False, "exec error: %s" % ex)
    out = (p.stdout or "") + (p.stderr or "")
    ok = (p.returncode == 0) and ('"messageId"' in out) and ('"error"' not in (p.stdout or ""))
    return (ok, out)


# --------------------------------------------------------------------- main ----
def main():
    dry = "--dry-run" in sys.argv[1:]
    today = date.today().isoformat()

    if not os.path.exists(IMAGE_PATH):
        sys.stderr.write("ERROR: promo image missing: %s\n" % IMAGE_PATH)
        return 1

    entries = load_targets()
    weekly = load_weekly_targets()
    # Weekly-limited groups are valid send targets too; fold them into the entry
    # list (so they get resolved/deduped), then gate them by weekday below.
    all_entries = entries + [j for j in weekly if j not in entries]
    if not all_entries:
        print("NO_REPLY")  # nothing configured -> stay silent
        return 0

    st = load_state()
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
        print("Image: %s" % IMAGE_PATH)
        print("\n----- message -----")
        print(POST_TEXT)
        print("-------------------")
        print("\n[dry-run] resolved=%d eligible=%d pending=%d"
              % (len(jids), len(eligible), len(pending)))
        return 0

    if not pending:
        print("NO_REPLY")  # every group already got today's ad
        return 0

    failures = 0
    skipped = 0
    sent = 0
    for i, jid in enumerate(pending):
        ok, out = send_ad(jid)
        if ok:
            sent_map[jid] = today
            save_state(st)  # commit per group: crash/retry never double-posts
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
            time.sleep(SEND_DELAY_SECONDS)

    save_state(st)  # persist any newly-cached invite resolutions

    print("SENT %d/%d (skipped %d, failures %d)" % (sent, len(pending), skipped, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
