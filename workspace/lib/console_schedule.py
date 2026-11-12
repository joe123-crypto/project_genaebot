"""Recurring start-time edits, audited desired state and verified gateway updates.

Every campaign is data-defined now, so a campaign's schedule is the ``schedule``
field on its own document and there is no declaration to fall back to. What went
with the manifest: the shipped cron and timezone a campaign without a
``scheduleOverride`` used to inherit, and the second gateway path that edited a
job whose argv was ``["python3", "<post script>"]``.
"""
import copy
import datetime as dt
import hashlib
import json
import re
import secrets
from zoneinfo import ZoneInfo

import campaign_deletion
import console_campaigns as campaigns
import console_config
import operator_config as config
import operator_writer as writer

DISPLAY_ZONE = "Africa/Harare"
TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def need(condition, message, status=400):
    if not condition:
        raise writer.WriteRefused(message, status=status)


def definition(doc):
    need(doc.get("kind") == "console", "unknown campaign", 404)
    return {"cron": campaigns.cron_expression(doc["schedule"]), "timezone": doc["schedule"]["timezone"]}


def parse(value):
    parts = value["cron"].split()
    need(len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit()
         and 0 <= int(parts[0]) < 60 and 0 <= int(parts[1]) < 24
         and parts[2:4] == ["*", "*"]
         and (parts[4] == "*" or re.fullmatch(r"[0-7](?:,[0-7])*", parts[4])),
         "This cron does not have one editable daily or weekly start time.")
    ZoneInfo(value["timezone"])
    return parts


def revision(doc):
    return hashlib.sha256(json.dumps([definition(doc), doc.get("scheduleEdit", {}).get("version")], sort_keys=True).encode()).hexdigest()


def occurrence(value, day):
    parts = parse(value)
    zone, display = ZoneInfo(value["timezone"]), ZoneInfo(DISPLAY_ZONE)
    # Find the native occurrence that belongs to this displayed campaign day.
    for offset in (-1, 0, 1):
        local_day = day + dt.timedelta(days=offset)
        if parts[4] != "*" and str((local_day.weekday() + 1) % 7) not in ["0" if d == "7" else d for d in parts[4].split(",")]:
            continue
        local = dt.datetime.combine(local_day, dt.time(int(parts[1]), int(parts[0])), zone)
        shown = local.astimezone(display)
        # Nonexistent DST times must not be presented as valid occurrences.
        if shown.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        if shown.date() == day:
            return shown.hour * 60 + shown.minute
    return None


def describe(doc, day):
    value = definition(doc)
    state = doc.get("scheduleEdit") or {}
    try:
        parse(value)
        editable, reason = True, None
        confirmed = state.get("confirmed") or value
        confirmed_minutes = occurrence(confirmed, day)
    except (writer.WriteRefused, ValueError, KeyError):
        editable, reason, confirmed_minutes = False, "This cron does not have one editable daily or weekly start time.", None
    return {"revision": revision(doc), "editable": editable,
            **({"reason": reason} if reason else {}),
            "status": "pending" if state.get("pending") else "ready", "confirmedMinutes": confirmed_minutes}


def read_fields(doc, day, slug=""):
    value = definition(doc)
    edit = describe(doc, day)
    if not edit["editable"]:
        return {"scheduleEdit": edit}
    parts = parse(value)
    import console_schedule_entries
    entry_rows = console_schedule_entries.rows(slug, doc, day)
    if "scheduleEntries" in doc:
        edit = {**edit, "editable": False, "reason": "Use the independent schedule entry controls."}
    return {"scheduleEntries": entry_rows, "cron": value["cron"], "timezone": value["timezone"],
            "hour": int(parts[1]), "minute": int(parts[0]), "scheduleMinutes": occurrence(value, day), "scheduleEdit": edit}


def move(value, time, day):
    parts = parse(value)
    need(occurrence(value, day) is not None, "This campaign has no occurrence on the displayed day.")
    hour, minute = map(int, time.split(":"))
    display = ZoneInfo(DISPLAY_ZONE)
    shown = dt.datetime.combine(day, dt.time(hour, minute), display)
    native = shown.astimezone(ZoneInfo(value["timezone"]))
    need(native.minute != 0, "Choose a posting time that is not on the hour.")
    parts[0], parts[1] = str(native.minute), str(native.hour)
    result = {"cron": " ".join(parts), "timezone": value["timezone"]}
    need(occurrence(result, day) == hour * 60 + minute,
         "That time would move this run outside its existing weekdays. Choose a time on the same scheduled day.")
    return result


def validate(data):
    if isinstance(data, dict) and "operationId" in data:
        import console_schedule_entries
        return console_schedule_entries.validate(data)
    need(isinstance(data, dict) and not set(data) - {"date", "timezone", "changes", "actor"}, "Invalid schedule request.")
    need(data.get("timezone") == DISPLAY_ZONE, "Invalid display timezone.")
    need(isinstance(data.get("date"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["date"]), "Invalid schedule day.")
    try:
        day = dt.date.fromisoformat(data["date"])
    except ValueError:
        raise writer.WriteRefused("Invalid schedule day.")
    need(isinstance(data.get("actor", ""), str) and len(data.get("actor", "")) <= 200, "Invalid actor.")
    rows = data.get("changes")
    need(isinstance(rows, list) and 1 <= len(rows) <= 100, "Choose 1 to 100 changed campaigns.")
    slugs = set()
    for row in rows:
        need(isinstance(row, dict) and set(row) == {"slug", "time", "expectedRevision"}, "Invalid campaign change.")
        slug = row.get("slug")
        need(isinstance(slug, str) and campaigns.SLUG.fullmatch(slug) and slug != "new" and slug not in slugs, "Invalid or duplicate campaign ID.")
        slugs.add(slug)
        need(isinstance(row["time"], str) and TIME.fullmatch(row["time"]), "Invalid start time.")
        need(isinstance(row["expectedRevision"], str) and re.fullmatch(r"[a-f0-9]{64}", row["expectedRevision"]), "Invalid schedule revision.")
    return day


def save(data, store, env=None):
    if isinstance(data, dict) and "operationId" in data:
        import console_schedule_entries
        return console_schedule_entries.save(data, store, env)
    day = validate(data)
    # Shared with retirement and reconciliation, so an old retry cannot restore
    # a retired job or mark a newer edit as applied.
    with campaign_deletion.scheduler_lock(env):
        store.begin()
        try:
            docs = []
            for row in data["changes"]:
                slug = row["slug"]
                existing = store.get("campaigns", slug)
                need(existing is not None and not existing.get("deleted"), "Campaign is missing or deleted: %s" % slug, 409)
                need("scheduleEntries" not in existing, "This campaign uses independent schedule entries. Reload with the updated console.", 409)
                current = definition(existing)
                desired = move(current, row["time"], day)
                # An identical desired time is a safe retry after a lost reply.
                if desired == current:
                    continue
                need(row["expectedRevision"] == revision(existing),
                     "The schedule changed while you were editing. Reset to load the latest times.", 409)
                after = copy.deepcopy(existing)
                after["scheduleEdit"] = {"version": secrets.token_hex(16), "pending": True,
                                         "confirmed": (existing.get("scheduleEdit") or {}).get("confirmed") or current}
                parts = parse(desired)
                after["schedule"]["time"] = "%02d:%02d" % (int(parts[1]), int(parts[0]))
                audit_id, audit = writer.audit_document(data.get("actor"), "campaigns", slug, existing, after)
                docs.extend([("campaigns", slug, after, True), ("audit", audit_id, audit, False)])
            if docs:
                with console_config.mutation(env):
                    store.commit(docs)
        finally:
            store.rollback()
    # Desired configuration is the acceptance boundary. Gateway work is bounded
    # by the worker; the HTTP request never runs one CLI call per edited row.
    return {"campaigns": [summary(row["slug"], store, day) for row in data["changes"]]}


def summary(slug, store, day):
    doc = store.get("campaigns", slug)
    need(doc is not None and not doc.get("deleted"), "Campaign was deleted while saving.", 409)
    fields = read_fields(doc, day, slug)
    minutes = fields.get("scheduleMinutes")
    need(minutes is not None, "Saved schedule has no occurrence on the displayed day.", 409)
    return {"slug": slug, "minutes": minutes, "scheduleEdit": fields["scheduleEdit"]}


def reconcile_one(slug, store, env=None, call=None):
    call = call or campaigns.gateway
    with campaign_deletion.scheduler_lock(env):
        doc = store.get("campaigns", slug)
        if not doc or doc.get("deleted") or not (doc.get("scheduleEdit") or {}).get("pending"):
            return
        if "scheduleEntries" in doc:
            import console_schedule_entries
            return console_schedule_entries.reconcile(slug, store, env, call)
        desired = definition(doc)
        campaigns._reconcile_one(slug, store, call)
        jobs = call(["list", "--all", "--json"]).get("jobs", [])
        name = campaigns.job_name(slug, doc)
        matches = [job for job in jobs if job.get("name") == name]
        need(len(matches) == 1 and matches[0].get("schedule", {}).get("expr") == desired["cron"]
             and matches[0].get("schedule", {}).get("tz") == desired["timezone"], "Gateway schedule verification is pending.", 503)
        store.begin()
        try:
            current = store.get("campaigns", slug)
            if not current or current.get("deleted") or revision(current) != revision(doc):
                return
            current["scheduleEdit"] = {**current["scheduleEdit"], "pending": False, "confirmed": desired}
            with console_config.mutation(env):
                store.commit([("campaigns", slug, current, True)])
        finally:
            store.rollback()


def reconcile_all(store, env=None, call=None, problems=None):
    pending = 0
    for slug, _ in store.list("campaigns", where={"fieldFilter": {
            "field": {"fieldPath": "scheduleEdit.pending"}, "op": "EQUAL", "value": {"booleanValue": True}}}):
        try:
            reconcile_one(slug, store, env, call)
        except Exception as ex:
            pending += 1
            if problems is not None:
                problems.append("%s: %s" % (slug, campaigns.reason(ex)))
    return pending
