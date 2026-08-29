#!/usr/bin/env python3
"""
Genaie-services daily ad poster.

Posts a FIXED, predefined advert (caption + image) for Genaie's own services
(WhatsApp group advertising, poster design, WhatsApp bots) to a configured set of
WhatsApp groups, once per day per group. There is NO AI/LLM generation involved:
the post text is a hardcoded constant and the image is a bundled asset. This
script is meant to run from an OpenClaw `--command` cron (deterministic, no agent
turn).

This script OWNS delivery: it sends each WhatsApp message itself via
`openclaw message send --media` and only records a group as "sent today" after a
confirmed send, so delivery and progress are transactional per group (same pattern
as cron/building-materials-promo/post_ad.py).

Targets come from targets.json (JIDs and/or invite links). Invite links are
resolved to group JIDs via the OpenClaw WhatsApp directory; the bot must already
be a MEMBER of each group for resolution and sending to work. Resolved JIDs are
cached in the state file to avoid re-resolving every day.

Usage:
  post_ad.py            normal run (resolve, skip groups already sent today, send)
  post_ad.py --dry-run  show resolved targets + the message; no send, no state
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
    "GENAIE_PROMO_TARGETS", os.path.join(HERE, "targets.json")
)
IMAGE_PATH = os.environ.get(
    "GENAIE_PROMO_IMAGE", os.path.join(HERE, "assets", "genaie-services.png")
)
STATE_FILE = os.environ.get(
    "GENAIE_PROMO_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/genaie-promo/state.json",
)
SEND_DELAY_SECONDS = float(os.environ.get("GENAIE_PROMO_SEND_DELAY", "3"))


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

# Fixed advert. Verbatim marketing copy supplied by the operator. Emoji are kept
# as explicit \U escapes so the source stays ASCII-safe.
POST_TEXT = (
    "Get Your Business Seen. Get More Customers.\n"
    "\n"
    "A great business means nothing if people don't know it exists.\n"
    "\n"
    "Genaie helps businesses become more visible and reach the right people "
    "through practical digital marketing and creative services.\n"
    "\n"
    "What we do:\n"
    "\n"
    "\U0001F4E2 WhatsApp Group Advertising — Put your business in front of "
    "active, targeted audiences.\n"
    "\U0001F3A8 Professional Poster Design — Turn your offer into visuals "
    "people notice and remember.\n"
    "\U0001F916 WhatsApp Bots — Automate customer interactions and make it "
    "easier to serve your customers.\n"
    "\n"
    "\U0001F4DE Message us on WhatsApp: +213563719936\n"
    "\n"
    "Genaie — helping you get noticed."
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
    if not entries:
        print("NO_REPLY")  # nothing configured -> stay silent
        return 0

    st = load_state()
    jids = resolve_targets(entries, st, dry=dry)

    if not jids:
        sys.stderr.write("No resolvable group targets.\n")
        print("NO_REPLY")
        return 0

    sent_map = st["sent"]
    pending = [j for j in jids if sent_map.get(j) != today]

    if dry:
        print("Targets (resolved JIDs): %d" % len(jids))
        for j in jids:
            mark = "already sent today" if sent_map.get(j) == today else "would send"
            print("  - %s  [%s]" % (j, mark))
        print("Image: %s" % IMAGE_PATH)
        print("\n----- message -----")
        print(POST_TEXT)
        print("-------------------")
        print("\n[dry-run] resolved=%d pending=%d" % (len(jids), len(pending)))
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
