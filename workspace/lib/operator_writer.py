"""Writing operator config to Firestore, validated by the bot's own rules.

:mod:`operator_config` reads the canonical store; this writes it. Both the seed
script (``deploy/seed-firestore.py``) and the console's write bridge
(``workspace/bin/console-write``) go through here, so there is one definition of
what a legal document is and one way a change reaches Firestore.

The write path is deliberately not "set these fields":

1. read every collection live, exactly as a campaign run does;
2. apply the patch to ONE document, field by field, names checked against
   :data:`EDITABLE`;
3. re-assemble the whole config and run it through
   ``operator_config.quarantine``, which is ``promo_membership.validate`` --
   the same code that decides whether a run may post to a group;
4. refuse if the edited document would be quarantined; otherwise commit it.

Step 3 is the point. The Firebase Console has no schema and no CI in front of
it, and a write that leaves a group quarantined is a group that silently stops
being advertised to. Validating the *prospective whole* also catches the
cross-document cases a per-field check cannot: pointing a group at a line that
does not exist, or clearing the contact account every campaign's caption needs.

What this cannot do, on purpose:

* **``rank`` is not editable.** It is send order -- which groups a short run
  reaches -- not a property of a group, so reordering is its own operation. A
  new document is appended after the current last.
* **No bulk writes.** One document per request, so a refusal names one thing
  and a partial apply cannot happen.
* **Only a group can be deleted through this generic writer** (:func:`delete`). The
  ordinary way out of the rotation is still ``status`` or ``paused_promos``, which
  keep the assignments and make resuming a flip. A delete removes the record, so
  the whole record goes into its audit document in the same commit: it is gone
  from the config, never from the trail. Pruning strays stays with the seed script.

Every change is committed **together with its audit document**, in one
Firestore commit, so a change can never land without the record of who made it.
Firestore keeps no history of a write and the Console bypasses this path
entirely, so this is the only trail there is.

Std-lib only, Python 3.11+.
"""

import datetime
import json
import os
import secrets
import sys
import time
import urllib.request

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import operator_config  # noqa: E402

# Rank in steps of ten, so a document added between two others needs no
# renumbering of its neighbours.
RANK_STEP = 10
# Firestore's limit on writes in one commit.
COMMIT_BATCH = 500
# Not one of operator_config.COLLECTIONS, so a reader never lists it.
AUDIT_COLLECTION = "audit"

# What a patch may name, per collection. Anything else is refused by name rather
# than written and found later by a quarantine report.
EDITABLE = {
    "groups": ("name", "source", "status", "activity", "account", "daily_cap",
               "promos", "paused_promos", "weekly", "note"),
    "lines": ("number",),
    "campaigns": ("enabled", "comment", "weekly_comment", "dir", "captions"),
    "settings": ("activity", "caps_enabled", "contact_account", "bands"),
}
# Generic document deletions. Campaign retirement uses campaign_deletion instead.
DELETABLE = ("groups",)
# A patch value has to survive a Firestore round trip and be something
# promo_membership.validate expects. Timestamps, bytes and references do not
# belong in this config; operator_config.decode_value refuses them on the way
# back in, so they are refused here on the way out.
SCALARS = (bool, int, float, str)


class WriteRefused(Exception):
    """The change was not made. ``problems`` says why, in operator terms."""

    def __init__(self, message, problems=None, status=400):
        super().__init__(message)
        self.problems = list(problems or [])
        self.status = status


# --------------------------------------------------------------------------
# Shaping documents (shared with the seed script)


def documents(membership, switches):
    """``{collection: [(doc_id, fields)]}`` -- plain decoded fields, in file
    order, each carrying a ``rank`` so Firestore's by-ID listing can be put back
    in order."""
    settings = dict((name, membership[name])
                    for name in operator_config.TOP_LEVEL_SETTINGS if name in membership)
    settings.update((k, v) for k, v in (membership.get("defaults") or {}).items()
                    if k != "accounts")

    def ranked(items):
        return [(doc_id, dict(data, rank=index * RANK_STEP))
                for index, (doc_id, data) in enumerate(items)]

    return {
        "settings": [(operator_config.SETTINGS_DOC, settings)],
        "lines": ranked(((membership.get("defaults") or {}).get("accounts") or {}).items()),
        "campaigns": ranked((slug, dict(meta, enabled=switches[slug]))
                            for slug, meta in (membership.get("promos") or {}).items()),
        "groups": ranked((membership.get("groups") or {}).items()),
    }


def plan_writes(desired, existing):
    """``(writes, strays, unchanged)``: documents to create or replace, documents
    Firestore has that the source does not, and how many already match."""
    writes, strays, unchanged = [], [], 0
    for collection in operator_config.COLLECTIONS:
        have = dict(existing.get(collection) or [])
        want = dict(desired.get(collection) or [])
        for doc_id, fields in want.items():
            if have.get(doc_id) == fields:
                unchanged += 1
            else:
                writes.append((collection, doc_id, fields, doc_id in have))
        strays.extend((collection, doc_id) for doc_id in have if doc_id not in want)
    return writes, strays, unchanged


def commit(project, token, writes, deletes, *, opener=None, sleep=None):
    """Apply full-document replaces and deletes, ``COMMIT_BATCH`` at a time.
    A replace without an update mask overwrites the whole document, which is what
    makes re-running the seed idempotent.

    ``writes`` are ``(collection, doc_id, fields)`` tuples, optionally with a
    fourth element saying whether the document already existed, which matters to
    the caller and not here."""
    opener = opener or urllib.request.urlopen
    sleep = sleep or time.sleep
    base = "projects/%s/databases/(default)/documents" % project
    operations = [{"update": {"name": "%s/%s/%s" % (base, write[0], write[1]),
                              "fields": operator_config.encode_fields(write[2])}}
                  for write in writes]
    operations += [{"delete": "%s/%s/%s" % (base, collection, doc_id)}
                   for collection, doc_id in deletes]
    url = "%s/%s:commit" % (operator_config.API_ROOT, base)
    for start in range(0, len(operations), COMMIT_BATCH):
        body = json.dumps({"writes": operations[start:start + COMMIT_BATCH]}).encode("utf-8")
        operator_config._request(url, opener=opener, sleep=sleep, data=body,
                                 method="POST",
                                 headers=dict(operator_config._auth(token),
                                              **{"Content-Type": "application/json"}))


# --------------------------------------------------------------------------
# Applying one patch


def _check_value(collection, name, value, path=""):
    """Refuse a value Firestore or the validator cannot take, naming the field.

    ``None`` is allowed: ``daily_cap`` and ``name`` are legitimately null, and
    ``nullValue`` round-trips."""
    where = "%s.%s" % (name, path) if path else name
    if value is None or isinstance(value, SCALARS):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_value(collection, name, item, "%s[%d]" % (path, index))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise WriteRefused("%s/%s: keys must be strings" % (collection, where))
            _check_value(collection, name, item, "%s.%s" % (path, key) if path else key)
        return
    raise WriteRefused("%s: %s is not a value this config can hold"
                       % (where, type(value).__name__))


def apply_fields(collection, current, patch):
    """``current`` with ``patch`` applied: named fields replaced, and a field set
    to ``None`` kept as a null rather than removed, because ``daily_cap: null``
    and ``name: null`` both mean something here.

    Refuses an unknown field name, and ``rank`` in particular, rather than
    writing it and leaving the operator to find out from a quarantine report."""
    allowed = EDITABLE.get(collection)
    if allowed is None:
        raise WriteRefused("%s is not an editable collection" % collection, status=404)
    if not isinstance(patch, dict) or not patch:
        raise WriteRefused("a change must name at least one field")
    unknown = sorted(set(patch) - set(allowed))
    if unknown:
        extra = (" (send order is not edited here)"
                 if operator_config.RANK in unknown else "")
        raise WriteRefused("%s cannot set %s%s; editable: %s"
                           % (collection, ", ".join(unknown), extra, ", ".join(allowed)))
    for name, value in patch.items():
        _check_value(collection, name, value)
    return dict(current, **patch)


def reinterpreted(collection, patch, fields):
    """Problems with values the reader would quietly read as something else.

    The whole-config check below catches anything that would quarantine a group.
    It cannot catch these, because the reader is deliberately forgiving about
    them and never complains: ``caps_enabled`` is read with ``bool()``, so the
    string "false" switches caps ON; a malformed band silently falls back to the
    default band; a campaign switch that is not a boolean reads as OFF. Forgiving
    is right when reading -- a capacity setting must never be why a day's ads do
    not go out -- and wrong when writing, where it would let a save succeed and
    then be ignored.

    Only the fields this change names are judged, so a bad value someone typed
    into the Firebase Console earlier cannot block an unrelated edit."""
    problems = []
    if collection == "campaigns" and "enabled" in patch:
        if not isinstance(patch["enabled"], bool):
            problems.append("enabled must be true or false, got %r" % (patch["enabled"],))
    if collection != "settings":
        return problems
    if "caps_enabled" in patch and not isinstance(patch["caps_enabled"], bool):
        problems.append("caps_enabled must be true or false, got %r"
                        % (patch["caps_enabled"],))
    if "bands" in patch:
        bands = patch["bands"]
        if not isinstance(bands, dict) or not bands:
            problems.append("bands must map each tier to [low, high]")
        else:
            for tier, pair in sorted(bands.items()):
                if (not isinstance(pair, list) or len(pair) != 2
                        or any(not isinstance(v, int) or isinstance(v, bool) for v in pair)):
                    problems.append("bands.%s must be [low, high] whole numbers, got %r"
                                    % (tier, pair))
                elif pair[0] < 0 or pair[0] > pair[1]:
                    problems.append("bands.%s must have 0 <= low <= high, got %r"
                                    % (tier, pair))
    if "activity" in patch:
        import promo_membership  # deferred, as in operator_config.quarantine
        tier = patch["activity"]
        # The tiers the reader will actually have: stored bands are overrides on
        # top of the built-in ones, so a store holding only {"busy": ...} still
        # has normal and quiet.
        known = set(promo_membership.bands({"defaults": {"bands": fields.get("bands")}}))
        if not isinstance(tier, str) or tier not in known:
            problems.append("activity must name a tier that has a band (%s), got %r"
                            % (", ".join(sorted(known)), tier))
    return problems


def next_rank(decoded):
    """Where a new document goes: after the current last. Order is send order, so
    appending is the one position that cannot change who an existing short run
    reaches."""
    ranks = [data[operator_config.RANK] for _, data in decoded
             if isinstance(data, dict)
             and isinstance(data.get(operator_config.RANK), (int, float))
             and not isinstance(data.get(operator_config.RANK), bool)]
    return (int(max(ranks)) + RANK_STEP) if ranks else 0


def _decoded(raw, collection):
    """``[(doc_id, fields)]`` for a collection, in rank order. A document that
    cannot be decoded at all is left as-is for :func:`operator_config.assemble`
    to quarantine or refuse; it is not this path's business unless it is the one
    being edited."""
    out = []
    for doc_id, fields in raw.get(collection) or []:
        try:
            out.append((doc_id, operator_config.decode_fields(fields)))
        except ValueError:
            out.append((doc_id, None))
    return out


def prepare(raw, collection, doc_id, patch):
    """``(fields, before, membership, report)`` for a change that is safe to
    commit, or :class:`WriteRefused`.

    ``fields`` is the whole document to write, ``before`` the document as it was
    (``None`` when creating), and ``report`` carries the warnings that are worth
    showing the operator but are no reason to refuse -- two groups sharing a
    name, say, which ``quarantine`` itself treats as a warning."""
    if collection not in EDITABLE:
        raise WriteRefused("%s is not an editable collection" % collection, status=404)
    import campaign_deletion
    deleted = campaign_deletion.deleted_slugs(raw)
    if collection == "campaigns" and doc_id in deleted:
        raise WriteRefused("this campaign has been deleted", status=409)
    if collection == "groups":
        mentioned = set()
        if isinstance(patch, dict):
            for value in (patch.get("promos"), patch.get("paused_promos"), patch.get("weekly")):
                if isinstance(value, (list, dict)):
                    mentioned.update(slug for slug in value if isinstance(slug, str))
        if mentioned & deleted:
            raise WriteRefused("cannot assign a deleted campaign", status=409)
    rows = dict(_decoded(raw, collection))
    before = rows.get(doc_id)
    if doc_id in rows and before is None:
        raise WriteRefused("%s/%s cannot be read; fix it in the Firebase Console"
                           % (collection, doc_id), status=409)
    if before is None and collection != "groups":
        # A line or a campaign is not something an operator invents from a form:
        # a new line needs a WhatsApp login, a new campaign needs code, copy, a
        # cron and an installer run.
        raise WriteRefused("%s/%s does not exist, and this path does not create "
                           "%s documents" % (collection, doc_id, collection), status=404)

    current = dict(before or {})
    if before is None:
        current[operator_config.RANK] = next_rank(_decoded(raw, collection))
    fields = apply_fields(collection, current, patch)
    problems = reinterpreted(collection, patch, fields)
    if problems:
        raise WriteRefused("this change would be read differently from how it was "
                           "written", problems=problems)

    # The prospective WHOLE config, assembled and judged exactly as a run would.
    after = dict(raw)
    rows_raw = [(other, body) for other, body in (raw.get(collection) or [])
                if other != doc_id]
    rows_raw.append((doc_id, operator_config.encode_fields(fields)))
    after[collection] = rows_raw
    try:
        membership, enabled, undecodable = operator_config.assemble(after)
        _, report = operator_config.quarantine(membership, undecodable)
    except operator_config.ConfigInvalid as ex:
        # Settings, a line or a campaign every group depends on: this would take
        # the whole config down, not one document.
        raise WriteRefused("this change would make the config unreadable: %s" % ex)
    quarantined = report.get("quarantined") or {}
    if doc_id in quarantined:
        raise WriteRefused("this change would stop %s being advertised to"
                           % doc_id, problems=quarantined[doc_id])
    # An edit to shared settings can quarantine OTHER groups: a renamed line
    # leaves every group that pointed at it invalid.
    others = sorted(set(quarantined) - {doc_id})
    if others and collection != "groups":
        raise WriteRefused(
            "this change would stop %d other group(s) being advertised to"
            % len(others), problems=["%s: %s" % (jid, "; ".join(quarantined[jid]))
                                     for jid in others])
    return fields, before, membership, report


# --------------------------------------------------------------------------
# The audit trail


def audit_document(actor, collection, doc_id, before, after, now=None, reason=None):
    """``(doc_id, fields)`` recording one change.

    It is committed with the change itself, so there is no window in which a
    write has landed and the record of it has not. ``before=None`` records a
    creation and ``after=None`` a deletion; a deletion's ``before`` is the whole
    record, which is what makes a delete recoverable at all."""
    now = now or time.time
    at = datetime.datetime.fromtimestamp(now(), datetime.timezone.utc)
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    changed = sorted(name for name in set(before or {}) | set(after or {})
                     if name != operator_config.RANK
                     and (before or {}).get(name) != (after or {}).get(name))
    if before is None:
        change = "created"
    elif after is None:
        change = "deleted"
    else:
        change = "updated"
    # Sortable, and unique even for two changes in the same second.
    entry_id = "%s-%s" % (at.strftime("%Y%m%dT%H%M%SZ"), secrets.token_hex(4))
    entry = {"at": stamp, "actor": actor or "unknown",
             "collection": collection, "id": doc_id, "change": change,
             "fields": changed,
             "before": before if before is not None else {},
             "after": after if after is not None else {}}
    if reason:
        entry["reason"] = reason
    return entry_id, entry


# --------------------------------------------------------------------------
# The entry point


def write(collection, doc_id, patch, actor, env=None, *, opener=None, signer=None,
          now=None, sleep=None, dry_run=False):
    """Validate and commit one change. Returns what happened::

        {"collection", "id", "change": "created" | "updated" | "unchanged",
         "fields": [names], "before": {...}, "after": {...},
         "warnings": [...], "audit": <audit doc id> | None}

    Raises :class:`WriteRefused` when the change is not safe to make, and
    :class:`operator_config.ConfigUnavailable` when the live config cannot be
    read at all -- a write must never be applied to a guess at the current state.
    """
    env = os.environ if env is None else env
    if collection == "campaigns" and "captions" in patch:
        if (env.get("GENAIE_CONFIG_SOURCE") or "").strip().lower() != "firestore":
            raise WriteRefused("caption editing requires Firestore", status=409)
        try:
            operator_config.validate_captions(patch["captions"])
        except operator_config.ConfigInvalid as ex:
            raise WriteRefused(str(ex)) from ex
    import campaign_deletion
    if campaign_deletion.enabled(env) and collection in ("groups", "campaigns"):
        return campaign_deletion.transactional_write(collection, doc_id, patch, actor, env, dry_run)
    now = now or time.time
    fetch_args = {"opener": opener, "signer": signer, "now": now, "sleep": sleep}
    # Changing one existing group cannot invalidate another group. Only its
    # record and shared validation dependencies are needed. Creation still
    # needs the existing ranks to append in the canonical sending order.
    project, raw = operator_config.fetch(env, **fetch_args,
                                         **({"group_ids": [doc_id]} if collection == "groups" else {}))
    if collection == "groups" and not raw["groups"]:
        project, raw = operator_config.fetch(env, **fetch_args)
    fields, before, _, report = prepare(raw, collection, doc_id, patch)
    warnings = list(report.get("warnings") or [])
    result = {"collection": collection, "id": doc_id, "fields": [], "before": before,
              "after": fields, "warnings": warnings, "audit": None,
              "change": "created" if before is None else "updated"}
    if before == fields:
        result["change"] = "unchanged"
        return result
    entry_id, entry = audit_document(actor, collection, doc_id, before, fields, now=now)
    result["fields"] = entry["fields"]
    if dry_run:
        return result

    token = operator_config.access_token(
        operator_config.credentials(env),
        operator_config.token_cache_path(operator_config.state_dir(env)),
        opener=opener or urllib.request.urlopen,
        signer=signer or operator_config.node_signer,
        now=now, sleep=sleep or time.sleep)
    # One commit, so the change and its record land together or not at all.
    commit(project, token,
           [(collection, doc_id, fields, before is not None),
            (AUDIT_COLLECTION, entry_id, entry, False)],
           [], opener=opener, sleep=sleep)
    result["audit"] = entry_id
    return result


def write_group_batch(changes, actor, env=None, *, dry_run=False, store=None):
    """Validate a bounded group edit against one live transactional snapshot.

    Shared rules are read once. No writes are issued until every change passes;
    changed groups and their audits fit in one atomic commit (at most 200 writes).
    """
    import re
    if (not isinstance(changes, list) or not 1 <= len(changes) <= 100
            or actor is not None and not isinstance(actor, str)):
        raise WriteRefused("batch must name 1..100 group changes and a valid actor")
    ids = []
    for change in changes:
        if (not isinstance(change, dict) or set(change) - {"id", "fields", "expected"}
                or not isinstance(change.get("id"), str)
                or not re.fullmatch(r"[0-9]+(?:-[0-9]+)?@g\.us", change["id"])
                or not isinstance(change.get("fields"), dict)
                or not isinstance(change.get("expected", {}), dict)
                or set(change.get("expected", {})) - set(change["fields"])):
            raise WriteRefused("invalid group batch change")
        ids.append(change["id"])
    if len(set(ids)) != len(ids):
        raise WriteRefused("batch names a group more than once")
    if store is None:
        from console_campaigns import Store
        store = Store(env)
    store.begin()
    try:
        settings = store.get("settings", operator_config.SETTINGS_DOC)
        raw = {"settings": [] if settings is None else [(operator_config.SETTINGS_DOC, operator_config.encode_fields(settings))],
               "lines": store.list("lines"), "campaigns": store.list("campaigns"), "groups": []}
        for jid in ids:
            try:
                fields = store.get("groups", jid)
            except ValueError as exc:
                raise WriteRefused("group %s cannot be read" % jid, status=409) from exc
            if fields is None:
                raise WriteRefused("group %s no longer exists" % jid, status=409)
            raw["groups"].append((jid, operator_config.encode_fields(fields)))
        docs = []
        for change in changes:
            jid = change["id"]
            fields, before, _, _ = prepare(raw, "groups", jid, change["fields"])
            for key, expected in change.get("expected", {}).items():
                current = before.get(key)
                if key in ("promos", "paused_promos"):
                    current = current or []
                elif key == "weekly":
                    current = current or {}
                if current != expected:
                    raise WriteRefused("group %s changed since the draft was checked" % jid, status=409)
            if before == fields:
                continue
            entry_id, entry = audit_document(actor, "groups", jid, before, fields)
            docs.extend([("groups", jid, fields, True), (AUDIT_COLLECTION, entry_id, entry, False)])
        if docs and not dry_run:
            try:
                store.commit(docs)
            except operator_config.ConfigUnavailable as exc:
                raise WriteRefused("The batch outcome is unknown. Reload before retrying; some or all changes may have been saved.", status=503) from exc
        return {"done": ids, "changed": len(docs) // 2}
    finally:
        store.rollback()


def prepare_delete(raw, collection, doc_id):
    """``(before, report)`` for a deletion that is safe to commit, or
    :class:`WriteRefused`.

    Only groups can go. A line still named by groups would quarantine every one of
    them; a campaign is code, copy, a cron and an installer run, not a record; and
    the settings document is the one every group is judged against."""
    if collection not in DELETABLE:
        raise WriteRefused("%s documents cannot be deleted here; only %s can"
                           % (collection, ", ".join(DELETABLE)), status=400)
    rows = dict(_decoded(raw, collection))
    if doc_id not in rows:
        raise WriteRefused("%s/%s does not exist" % (collection, doc_id), status=404)
    before = rows[doc_id]
    if before is None:
        # The audit document is what makes a delete recoverable; with a record we
        # cannot read, there would be nothing to recover it from.
        raise WriteRefused("%s/%s cannot be read, so it could not be recorded "
                           "before deleting; fix or remove it in the Firebase "
                           "Console" % (collection, doc_id), status=409)
    # Judged as the whole prospective config, exactly as an edit is. Removing a
    # group cannot quarantine another, but the same gate keeps the two paths from
    # drifting apart.
    after = dict(raw)
    after[collection] = [(other, body) for other, body in (raw.get(collection) or [])
                         if other != doc_id]
    try:
        membership, _, undecodable = operator_config.assemble(after)
        _, report = operator_config.quarantine(membership, undecodable)
    except operator_config.ConfigInvalid as ex:
        raise WriteRefused("this change would make the config unreadable: %s" % ex)
    return before, report


def delete(collection, doc_id, actor, env=None, *, reason=None, opener=None,
           signer=None, now=None, sleep=None, dry_run=False):
    """Delete one document, recording the whole of it first. Returns::

        {"collection", "id", "change": "deleted", "fields": [names],
         "before": {...}, "after": None, "warnings": [...],
         "audit": <audit doc id> | None}

    The deletion and its audit document -- which carries the complete record and
    the optional ``reason`` -- go in ONE commit, so a group can never vanish
    without the trail that would let it be recreated. Raises the same way
    :func:`write` does."""
    env = os.environ if env is None else env
    now = now or time.time
    project, raw = operator_config.fetch(env, opener=opener, signer=signer,
                                         now=now, sleep=sleep, group_ids=[doc_id])
    before, report = prepare_delete(raw, collection, doc_id)
    entry_id, entry = audit_document(actor, collection, doc_id, before, None,
                                     now=now, reason=reason)
    result = {"collection": collection, "id": doc_id, "change": "deleted",
              "fields": entry["fields"], "before": before, "after": None,
              "warnings": list(report.get("warnings") or []), "audit": None}
    if dry_run:
        return result

    token = operator_config.access_token(
        operator_config.credentials(env),
        operator_config.token_cache_path(operator_config.state_dir(env)),
        opener=opener or urllib.request.urlopen,
        signer=signer or operator_config.node_signer,
        now=now, sleep=sleep or time.sleep)
    commit(project, token, [(AUDIT_COLLECTION, entry_id, entry, False)],
           [(collection, doc_id)], opener=opener, sleep=sleep)
    result["audit"] = entry_id
    return result
