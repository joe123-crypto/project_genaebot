"""Single source of truth for the WhatsApp groups the bot advertises in.

``workspace/cron/membership.json`` lists every group the bot knows about once,
with its name, posting status, activity tier and the promos that target it. The
seven ``workspace/cron/*-promo/targets.json`` files are GENERATED from it by
``workspace/bin/make-promo-targets``; CI fails if they drift
(``deploy/test_promo_targets_generated.py``). Edit membership.json and
regenerate -- never hand-edit a targets file except to unblock production, and
fold the change back afterwards.

This replaces ``workspace/cron/group-names.json``. Automatic discovery saves
group IDs and names in runtime/whatsapp-groups for the console's Unassigned
section, independently of campaign targeting. For a manual probe, use the
running gateway's ``conversations.list`` RPC with
``agentId="main"`` and ``channel="whatsapp"``; see README's "Discovering
WhatsApp group IDs and names live" for the tested command and limitations.
Sender-key files alone can miss new joins and do not contain group names.
Never hand-edit names into the gateway's session database (doing so took the
bot down on 2026-10-14). Record the advertising roster in ONE place here.

Schema (``version: 1``)::

    {
      "version": 1,
      "defaults": {"activity": "normal", "caps_enabled": false,
                   "contact_account": "ads",
                   "bands": {"busy": [6, 10], "normal": [4, 6], "quiet": [1, 3]},
                   "accounts": {"default": {"number": "+263785632943"},
                                "ads": {"number": "+2637..."}}},
      "promos": {"cv": {"comment": "...", "weekly_comment": "..."}, ...},
      "groups": {
        "<jid>": {
          "name": "Buying and Selling" | null,
          "source": "whatsapp" | "operator" | "git",
          "status": "active" | "on-hold" | "admin-only" | "not-a-member",
          "activity": "busy" | "normal" | "quiet",
          "account": "default" | "ads",   # which WhatsApp line posts here
          "daily_cap": null | int,
          "promos": ["cv", "genaie", ...],
          "weekly": {"cv": ["mon", "thu"]},
          "note": "free-text, optional"
        }
      }
    }

``defaults.accounts`` maps each WhatsApp line the bot advertises from to the
number that identifies it. ``defaults.contact_account`` optionally selects the
shared advertised contact, independent of the sending account. A group's ``account`` names
its line; absent ⇒ the ``default`` line. Spreading groups across two accounts
keeps any one account from being a member of a suspiciously large number of
groups.

``status`` is what gates generation: only ``active`` groups reach a targets
file. ``promos`` is the *intent* and is kept even while a group is paused, so
flipping ``status`` back to ``active`` restores it without rediscovering which
campaigns it belonged to.

Std-lib only (plus the sibling :mod:`promo_broadcast` for weekday parsing), so
it stays importable from the OpenClaw ``--command`` cron scripts.
"""

import os
import json

import promo_broadcast

# Promo keys are the targets directory name minus the "-promo" suffix, which is
# also the subdirectory each generated file lives in.
PROMO_DIR_SUFFIX = "-promo"
STATUSES = ("active", "on-hold", "admin-only", "not-a-member")
SOURCES = ("whatsapp", "operator", "git")
DEFAULT_ACTIVITY = "normal"
DEFAULT_BANDS = {"busy": (6, 10), "normal": (4, 6), "quiet": (1, 3)}
# The bot's original WhatsApp line. A group with no explicit ``account`` is
# advertised from, and advertises, this line; a second line is tagged per group
# under ``defaults.accounts`` so the group-load can be split across two numbers.
DEFAULT_ACCOUNT = "default"


def default_path(workspace=None):
    """Path to membership.json. ``workspace`` defaults to this file's parent
    workspace, so it resolves both in the repo and on the gateway volume."""
    if workspace is None:
        workspace = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(workspace, "cron", "membership.json")


def load(path=None):
    """Return the parsed membership file, or ``None`` when it is absent or
    unreadable. Callers treat ``None`` as "no membership data" and fall back to
    their previous behaviour -- that is what keeps the caps inert until the file
    is deployed."""
    path = path or default_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError):
        return None
    except json.JSONDecodeError as ex:
        raise ValueError("membership file is not valid JSON: %s" % ex)
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        raise ValueError("membership file has no 'groups' object")
    return data


def groups(m):
    """``{jid: entry}`` in file order (the canonical order for generated files)."""
    return dict((m or {}).get("groups") or {})


def promo_names(m):
    """Every promo key named anywhere in the file, sorted. Includes keys that
    only appear under ``promos``/``weekly`` of a group, so a promo cannot be
    silently invisible to validation by lacking a ``promos`` section."""
    out = set((m or {}).get("promos") or {})
    for entry in groups(m).values():
        out.update(entry.get("promos") or [])
        out.update(entry.get("weekly") or {})
    return sorted(out)


def status_of(entry):
    return str(entry.get("status") or "active")


def is_active(entry):
    return status_of(entry) == "active"


def activity_of(m, entry):
    """The group's activity tier, falling back to the file default."""
    tier = entry.get("activity")
    if tier:
        return str(tier)
    return str(((m or {}).get("defaults") or {}).get("activity") or DEFAULT_ACTIVITY)


def bands(m):
    """``{tier: (low, high)}`` daily-cap bands, defaults filled in."""
    raw = ((m or {}).get("defaults") or {}).get("bands") or {}
    out = {}
    for tier, pair in DEFAULT_BANDS.items():
        got = raw.get(tier)
        if (isinstance(got, (list, tuple)) and len(got) == 2
                and all(isinstance(v, int) and not isinstance(v, bool) for v in got)
                and got[0] <= got[1]):
            out[tier] = (int(got[0]), int(got[1]))
        else:
            out[tier] = pair
    for tier, pair in raw.items():
        if tier not in out and (isinstance(pair, (list, tuple)) and len(pair) == 2
                                and all(isinstance(v, int) and not isinstance(v, bool)
                                        for v in pair) and pair[0] <= pair[1]):
            out[str(tier)] = (int(pair[0]), int(pair[1]))
    return out


def caps_enabled(m):
    """Whether the per-group daily cap is active. Defaults to False, so
    deploying membership.json alone changes no sending behaviour."""
    return bool(((m or {}).get("defaults") or {}).get("caps_enabled"))


def accounts(m):
    """``{account_id: {"number": "+263..."}}`` from ``defaults.accounts``. The
    WhatsApp lines used to send ads, with their actual phone numbers.
    Empty when none are configured (then every group stays on the implicit
    default line and no per-account number is emitted)."""
    raw = ((m or {}).get("defaults") or {}).get("accounts") or {}
    out = {}
    if isinstance(raw, dict):
        for acct, cfg in raw.items():
            acct = str(acct).strip()
            if acct and isinstance(cfg, dict):
                out[acct] = cfg
    return out


def account_numbers(m):
    """``{account_id: advertised_number}`` for every configured account, in file
    order. This is carried verbatim into each targets.json so the send engine can
    resolve the shared contact account or, when absent, the posting account."""
    out = {}
    for acct, cfg in accounts(m).items():
        number = str(cfg.get("number") or "").strip()
        if number:
            out[acct] = number
    return out


def account_of(entry):
    """The WhatsApp account a group is advertised from. Absent ⇒ the default
    line."""
    return str((entry or {}).get("account") or DEFAULT_ACCOUNT)


def promos_for_group(m, jid):
    """Promo keys targeting ``jid``, in the order listed on the group. Empty for
    a group whose status is not ``active`` -- a paused group is in no campaign
    today even though its ``promos`` intent is preserved."""
    entry = groups(m).get(jid)
    if not entry or not is_active(entry):
        return []
    return [str(p) for p in (entry.get("promos") or [])]


def groups_for_promo(m, promo):
    """``(jids, weekly)`` for ``promo`` in the exact shape targets.json uses:
    ``jids`` is the daily list in membership order, and ``weekly`` is
    ``{jid: [weekday tokens]}`` for groups this promo posts to only on certain
    weekdays. A weekly-limited group is NOT repeated in ``jids`` (the engine
    folds the two together itself)."""
    jids = []
    weekly = {}
    for jid, entry in groups(m).items():
        if not is_active(entry) or promo not in (entry.get("promos") or []):
            continue
        days = (entry.get("weekly") or {}).get(promo)
        if days:
            weekly[jid] = [str(d) for d in days]
        else:
            jids.append(jid)
    return jids, weekly


def targets_payload(m, promo):
    """The full targets.json object for ``promo``, comments included."""
    meta = ((m or {}).get("promos") or {}).get(promo) or {}
    jids, weekly = groups_for_promo(m, promo)
    payload = {}
    if meta.get("comment"):
        payload["_comment"] = meta["comment"]
    payload["groups"] = jids
    if weekly:
        if meta.get("weekly_comment"):
            payload["_weekly_comment"] = meta["weekly_comment"]
        payload["weekly_groups"] = weekly
    # Per-group account tag (only for groups not on the default line) and the
    # account->number map. Contact routing is independent of sending.
    all_groups = groups(m)
    acct_map = {}
    for jid in list(jids) + list(weekly):
        acct = account_of(all_groups.get(jid) or {})
        if acct != DEFAULT_ACCOUNT:
            acct_map[jid] = acct
    if acct_map:
        payload["accounts"] = acct_map
    numbers = account_numbers(m)
    if numbers:
        payload["account_numbers"] = numbers
    contact_account = ((m or {}).get("defaults") or {}).get("contact_account")
    if contact_account is not None:
        payload["contact_account"] = contact_account
    return payload


def targets_path(m, promo, cron_dir):
    """Where ``promo``'s generated targets file lives."""
    meta = ((m or {}).get("promos") or {}).get(promo) or {}
    directory = meta.get("dir") or (promo + PROMO_DIR_SUFFIX)
    return os.path.join(cron_dir, directory, "targets.json")


def dumps_targets(payload):
    """Serialize a targets payload the way the hand-written files are formatted:
    two-space indent, UTF-8 (not \\u escapes), a trailing newline, and each
    weekly day list on one line while the main ``groups`` array stays one JID
    per line. Byte-stability is what lets CI diff a regenerated file against the
    committed one.

    Each day list is swapped for a sentinel string before dumping and the
    sentinel's quoted form is replaced by the inline array afterwards, so the
    one-line rendering never depends on matching indentation by hand."""
    inline = {}
    weekly = payload.get("weekly_groups") or {}
    if weekly:
        payload = dict(payload)
        payload["weekly_groups"] = swapped = {}
        for i, (jid, days) in enumerate(weekly.items()):
            token = "@@WEEKLY%d@@" % i
            swapped[jid] = token
            inline['"%s"' % token] = json.dumps(list(days), ensure_ascii=False)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    for token, days in inline.items():
        text = text.replace(token, days)
    return text + "\n"


def validate(m):
    """Return a list of human-readable problems; empty means the file is sound.
    Used by the generator (which refuses to write a broken file) and by
    ``deploy/test_promo_membership.py``."""
    errors = []
    if not isinstance(m, dict):
        return ["membership file is not an object"]
    if m.get("version") != 1:
        errors.append("unsupported version: %r (expected 1)" % m.get("version"))
    known = set(promo_names(m))
    declared = set((m.get("promos") or {}))
    for promo in sorted(known - declared):
        errors.append("promo %r is used by a group but not declared under 'promos'"
                      % promo)
    raw_accounts = ((m or {}).get("defaults") or {}).get("accounts")
    if raw_accounts is not None and not isinstance(raw_accounts, dict):
        errors.append("defaults.accounts must be an object")
        raw_accounts = {}
    for acct, cfg in (raw_accounts or {}).items():
        if not isinstance(cfg, dict):
            errors.append("defaults.accounts[%r] must be an object" % acct)
            continue
        number = cfg.get("number")
        if not isinstance(number, str) or not number.strip():
            errors.append("defaults.accounts[%r] must have a non-empty string "
                          "'number'" % acct)
    contact_account = ((m or {}).get("defaults") or {}).get("contact_account")
    if contact_account is not None:
        if (not isinstance(contact_account, str)
                or contact_account not in account_numbers(m)):
            errors.append("defaults.contact_account must name an account with a number")
    declared_accounts = set(accounts(m)) | {DEFAULT_ACCOUNT}
    seen_names = {}
    for jid, entry in groups(m).items():
        where = "group %s" % jid
        if not isinstance(entry, dict):
            errors.append("%s: entry is not an object" % where)
            continue
        if not str(jid).endswith("@g.us"):
            errors.append("%s: JID does not end in @g.us" % where)
        status = status_of(entry)
        if status not in STATUSES:
            errors.append("%s: unknown status %r (expected one of %s)"
                          % (where, status, ", ".join(STATUSES)))
        source = entry.get("source")
        if source is not None and str(source) not in SOURCES:
            errors.append("%s: unknown source %r (expected one of %s)"
                          % (where, source, ", ".join(SOURCES)))
        account = entry.get("account")
        if account is not None and str(account) not in declared_accounts:
            errors.append("%s: unknown account %r (declare it under "
                          "defaults.accounts; known: %s)"
                          % (where, account, ", ".join(sorted(declared_accounts))))
        tier = activity_of(m, entry)
        if tier not in bands(m):
            errors.append("%s: unknown activity tier %r (no band defined)"
                          % (where, tier))
        cap = entry.get("daily_cap")
        if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool)
                                or cap < 0):
            errors.append("%s: daily_cap must be null or a non-negative int, got %r"
                          % (where, cap))
        promos = entry.get("promos")
        if promos is not None and not isinstance(promos, list):
            errors.append("%s: promos must be a list" % where)
            promos = []
        if len(set(promos or [])) != len(promos or []):
            errors.append("%s: promos has duplicates: %r" % (where, promos))
        for promo in promos or []:
            if promo not in declared:
                errors.append("%s: unknown promo %r" % (where, promo))
        weekly = entry.get("weekly") or {}
        if not isinstance(weekly, dict):
            errors.append("%s: weekly must be an object" % where)
            weekly = {}
        for promo, days in weekly.items():
            if promo not in (promos or []):
                errors.append("%s: weekly names promo %r which is not in its promos"
                              % (where, promo))
            if not isinstance(days, list) or not days:
                errors.append("%s: weekly[%r] must be a non-empty list" % (where, promo))
                continue
            for token in days:
                if promo_broadcast._parse_weekday(token) is None:
                    errors.append("%s: weekly[%r] has invalid weekday %r"
                                  % (where, promo, token))
        name = entry.get("name")
        if name:
            if name in seen_names:
                errors.append("%s: name %r is also used by %s (names should be "
                              "unique so the operator can tell them apart)"
                              % (where, name, seen_names[name]))
            else:
                seen_names[name] = jid
    return errors
