import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import campaign_deletion
import console_campaigns as campaigns
import console_config
import console_schedule as schedule
import operator_writer as writer

IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def entries(doc):
    return copy.deepcopy(doc.get("scheduleEntries") if "scheduleEntries" in doc else {
        "primary": {**schedule.definition(doc), "paused": False, "deleted": False, "skipDates": []}})


def revision(doc):
    desired = {key: {field: value for field, value in entry.items() if field != "confirmed"}
               for key, entry in entries(doc).items()}
    return hashlib.sha256(json.dumps([desired, doc.get("enabled", True)], sort_keys=True).encode()).hexdigest()


def state(entry, day):
    minute = schedule.occurrence(entry, day)
    if minute is None:
        return None
    return {"minutes": minute, "paused": entry.get("paused", False),
            "skipped": str(day) in entry.get("skipDates", []), "deleted": entry.get("deleted", False)}


def rows(slug, doc, day, include_deleted=False):
    result = []
    current_revision = revision(doc)
    for key, entry in entries(doc).items():
        current = state(entry, day)
        if current is None or (current["deleted"] and not include_deleted):
            continue
        confirmed_entry = entry.get("confirmed") if "scheduleEntries" in doc else {**entry, **(doc.get("scheduleEdit", {}).get("confirmed") or {})}
        confirmed = state(confirmed_entry, day) if confirmed_entry else None
        parts = schedule.parse(entry)
        weekdays = list(range(7)) if parts[4] == "*" else sorted({(int(value) + 6) % 7 for value in parts[4].split(",")})
        result.append({"slug": slug, "entryId": key, **current, "time": "%02d:%02d" % divmod(current["minutes"], 60),
            "campaignEnabled": doc.get("enabled", True), "recurrence": {"timezone": entry["timezone"], "weekdays": weekdays},
            "confirmed": confirmed, "scheduleEdit": {"revision": current_revision, "editable": True,
                "status": "ready" if confirmed_entry == {field: value for field, value in entry.items() if field != "confirmed"} else "pending",
                "confirmedMinutes": confirmed["minutes"] if confirmed else None}})
    return result


def validate(data):
    schedule.need(isinstance(data, dict) and not set(data) - {"operationId", "date", "timezone", "changes", "actor"}, "Invalid schedule request.")
    schedule.need(isinstance(data.get("operationId"), str) and IDENTIFIER.fullmatch(data["operationId"]), "Invalid operation ID.")
    schedule.need(data.get("timezone") == schedule.DISPLAY_ZONE, "Invalid display timezone.")
    schedule.need(isinstance(data.get("date"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["date"]), "Invalid date.")
    day = dt.date.fromisoformat(data["date"])
    schedule.need(isinstance(data.get("actor", ""), str) and len(data.get("actor", "")) <= 200, "Invalid actor.")
    changes = data.get("changes")
    schedule.need(isinstance(changes, list) and 1 <= len(changes) <= 100, "Choose 1 to 100 changed entries.")
    seen = set()
    for change in changes:
        schedule.need(isinstance(change, dict) and set(change) == {"slug", "entryId", "expectedRevision", "create", "time", "paused", "skipped", "deleted"}, "Invalid entry change.")
        for field in ("slug", "entryId"):
            schedule.need(isinstance(change[field], str) and IDENTIFIER.fullmatch(change[field]), "Invalid entry ID.")
        key = (change["slug"], change["entryId"])
        schedule.need(key not in seen and change["slug"] != "new", "Duplicate entry change.")
        seen.add(key)
        schedule.need(isinstance(change["expectedRevision"], str) and re.fullmatch(r"[a-f0-9]{64}", change["expectedRevision"]), "Invalid revision.")
        schedule.need(isinstance(change["time"], str) and schedule.TIME.fullmatch(change["time"]), "Invalid start time.")
        schedule.need(all(type(change[field]) is bool for field in ("create", "paused", "skipped", "deleted")), "Invalid entry state.")
        schedule.need(not change["create"] or (change["entryId"] != "primary" and not change["deleted"]), "Invalid new entry.")
    return day


def save(data, store, env=None):
    day = validate(data)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    with campaign_deletion.scheduler_lock(env):
        store.begin()
        try:
            receipt = store.get("schedule_operations", data["operationId"])
            if receipt:
                schedule.need(receipt["digest"] == digest, "Operation ID was already used for a different request.", 409)
                return receipt["result"]
            originals, updated = {}, {}
            for change in data["changes"]:
                slug, key = change["slug"], change["entryId"]
                if slug not in originals:
                    doc = store.get("campaigns", slug)
                    schedule.need(doc is not None and not doc.get("deleted"), "Campaign is missing or deleted.", 409)
                    originals[slug] = doc
                    updated[slug] = copy.deepcopy(doc)
                    initial = entries(doc)
                    if "scheduleEntries" not in doc:
                        for entry in initial.values():
                            entry["confirmed"] = {**copy.deepcopy(entry), **(doc.get("scheduleEdit", {}).get("confirmed") or {})}
                    updated[slug]["scheduleEntries"] = initial
                doc = originals[slug]
                schedule.need(change["expectedRevision"] == revision(doc), "The schedule changed while you were editing. Reset to load the latest entries.", 409)
                desired = updated[slug]["scheduleEntries"]
                if change["create"]:
                    schedule.need(key not in desired and len(desired) < 100, "Entry already exists or campaign has reached 100 entries.", 409)
                    entry = {"cron": "0 0 * * *", "timezone": data["timezone"], "paused": False, "deleted": False, "skipDates": [], "confirmed": None}
                else:
                    schedule.need(key in desired and not desired[key].get("deleted"), "Scheduled entry is missing or deleted.", 409)
                    entry = copy.deepcopy(desired[key])
                hour, minute = map(int, change["time"].split(":"))
                native = dt.datetime.combine(day, dt.time(hour, minute), ZoneInfo(data["timezone"])).astimezone(ZoneInfo(entry["timezone"]))
                parts = schedule.parse(entry)
                parts[:2] = [str(native.minute), str(native.hour)]
                entry["cron"] = " ".join(parts)
                schedule.need(schedule.occurrence(entry, day) == hour * 60 + minute, "The selected time is outside this entry's weekdays.")
                entry.update(paused=change["paused"], deleted=change["deleted"])
                dates = set(entry.get("skipDates", []))
                if change["skipped"]:
                    dates.add(str(day))
                else:
                    dates.discard(str(day))
                entry["skipDates"] = sorted(dates)
                desired[key] = entry
                updated[slug]["scheduleEdit"] = {**doc.get("scheduleEdit", {}), "pending": True}
            result = {"operationId": data["operationId"], "entries": []}
            writes = []
            for slug, after in updated.items():
                by_id = {entry["entryId"]: entry for entry in rows(slug, after, day, True)}
                result["entries"].extend(by_id[change["entryId"]] for change in data["changes"] if change["slug"] == slug)
                audit_id, audit = writer.audit_document(data.get("actor"), "campaigns", slug, originals[slug], after)
                writes.extend([("campaigns", slug, after, True), ("audit", audit_id, audit, False)])
            writes.append(("schedule_operations", data["operationId"], {"digest": digest, "result": result}, False))
            with console_config.mutation(env):
                store.commit(writes)
            return result
        finally:
            store.rollback()


def command(slug, key):
    return ["python3", str(Path(__file__).resolve().parents[1] / "bin" / "run-schedule-entry"), slug, key]


def job_name(slug, key, doc):
    if key == "primary":
        return campaigns.job_name(slug, doc)
    return "console-entry-%s-%s" % (slug, key)


def apply_jobs(slug, doc, call):
    for key, entry in entries(doc).items():
        name = job_name(slug, key, doc)
        found = [job for job in call(["list", "--all", "--json"]).get("jobs", []) if job.get("name") == name]
        schedule.need(len(found) <= 1, "Duplicate entry jobs require operator review.", 409)
        argv = command(slug, key)
        allowed_commands = [argv]
        if key == "primary":
            allowed_commands.append(["python3", str(Path(__file__).resolve().parents[1] / "bin" / "run-console-campaign"), slug])
        if found:
            schedule.need(found[0].get("payload", {}).get("argv") in allowed_commands, "Entry job command does not match its owner.", 409)
        if entry.get("deleted"):
            if found:
                call(["rm", found[0]["id"]])
            schedule.need(not any(job.get("name") == name for job in call(["list", "--all", "--json"]).get("jobs", [])), "Deleted job is still registered.", 503)
            continue
        enabled = doc.get("enabled", True) and not entry.get("paused", False)
        timeout = doc.get("timeoutSeconds", 7200)
        alert = doc.get("failureAlertTo", "")
        flags = ["--name", name, "--cron", entry["cron"], "--tz", entry["timezone"], "--exact",
                 "--session", "isolated", "--command-argv", json.dumps(argv), "--command-cwd", str(Path(argv[1]).parents[1]),
                 "--timeout-seconds", str(timeout), "--no-deliver"]
        # Only cron edit takes the failure-alert options, so a created job is
        # given its alert by the edit that follows, never by the add itself.
        alert_flags = ["--failure-alert", "--failure-alert-after", "1", "--failure-alert-channel", "whatsapp",
                       "--failure-alert-to", alert, "--failure-alert-cooldown", "1h"] if alert else []
        if found:
            call(["edit", found[0]["id"]] + flags + alert_flags + (["--enable"] if enabled else ["--disable"]))
        else:
            call(["add"] + flags + ["--disabled", "--json"])
            found = [job for job in call(["list", "--all", "--json"]).get("jobs", []) if job.get("name") == name]
            schedule.need(len(found) == 1, "New entry job was not found uniquely.", 503)
            if alert_flags:
                call(["edit", found[0]["id"]] + alert_flags)
            if enabled:
                call(["edit", found[0]["id"], "--enable"])
        verified = [job for job in call(["list", "--all", "--json"]).get("jobs", []) if job.get("name") == name]
        schedule.need(len(verified) == 1, "Entry job verification failed.", 503)
        job = verified[0]
        schedule.need(job.get("enabled") == enabled and job.get("schedule", {}).get("expr") == entry["cron"] and job.get("schedule", {}).get("tz") == entry["timezone"]
                      and job.get("payload", {}).get("argv") == argv and job.get("payload", {}).get("timeoutSeconds") == timeout
                      and job.get("payload", {}).get("cwd") == str(Path(argv[1]).parents[1])
                      and (not alert or job.get("failureAlert", {}).get("to") == alert), "Entry job verification is pending.", 503)


def reconcile(slug, store, env=None, call=None):
    call = call or campaigns.gateway
    doc = store.get("campaigns", slug)
    if not doc or doc.get("deleted") or "scheduleEntries" not in doc:
        return
    apply_jobs(slug, doc, call)
    store.begin()
    try:
        current = store.get("campaigns", slug)
        if not current or current.get("deleted") or revision(current) != revision(doc):
            return
        changed = copy.deepcopy(current)
        for entry in changed["scheduleEntries"].values():
            entry["confirmed"] = {field: value for field, value in entry.items() if field != "confirmed"}
        changed["scheduleEdit"] = {**changed.get("scheduleEdit", {}), "pending": False}
        if changed.get("kind") == "console":
            changed["scheduleStatus"] = "ready"
        if changed != current:
            with console_config.mutation(env):
                store.commit([("campaigns", slug, changed, True)])
    finally:
        store.rollback()


def allowed(doc, key, day):
    if doc.get("deleted") or doc.get("enabled") is False:
        return False
    if "scheduleEntries" not in doc:
        return key == "primary"
    entry = doc["scheduleEntries"].get(key)
    if not entry:
        return False
    current = state(entry, day)
    return current is not None and not any(current[field] for field in ("paused", "skipped", "deleted"))


def run(slug, key, argv=None, now=None, read=None, execute=None):
    import fcntl
    import subprocess
    schedule.need(isinstance(slug, str) and IDENTIFIER.fullmatch(slug) and isinstance(key, str) and IDENTIFIER.fullmatch(key), "Invalid scheduled entry.")
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(ZoneInfo(schedule.DISPLAY_ZONE))
    day = local.date()
    doc = (read or campaign_deletion.live_campaign)(slug)
    if not allowed(doc, key, day):
        return 0
    entry = doc.get("scheduleEntries", {}).get(key)
    if entry and schedule.occurrence(entry, day) > local.hour * 60 + local.minute:
        return 0
    args = ["python3", str(Path(__file__).resolve().parents[1] / "bin" / "run-console-campaign"), slug]
    directory = Path(campaign_deletion.config.state_dir()) / "runtime" / "schedule-entries"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (slug + "-" + key + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        environment = {**os.environ, "GENAIE_SCHEDULE_ENTRY": key, "GENAIE_SCHEDULE_DAY": str(day)}
        return (execute or subprocess.call)(args + (argv or []), env=environment)
