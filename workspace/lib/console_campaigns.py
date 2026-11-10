"""Data-defined campaigns: validated atomic creation and recoverable cron setup.

The gateway API owns writes. Runs use only the read key and existing broadcast
engine. Files, job names, object keys and command argv are generated here, never
accepted as executable input from the console.
"""
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import ad_assets
import group_cleanup
import operator_config as config
import operator_writer as writer
import promo_config
import promo_membership

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
JOB_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_REQUEST = 60 * 1024
# Planning allowance per send for the spacing rule below. Deliberately the
# pessimistic figure the repository's CI used while campaign target lists were
# committed files, so moving the rule here changes no campaign's group ceiling.
SPACING_SEND_SECONDS = 60


def need(condition, message):
    if not condition:
        raise writer.WriteRefused(message)


def valid_days(days):
    return (isinstance(days, list) and 0 < len(days) <= 7
            and all(type(day) is int and 0 <= day <= 6 for day in days)
            and len(set(days)) == len(days))


def job_name(slug, campaign=None):
    """The gateway cron job, and the ``runtime/cron-state`` directory, a campaign
    runs under.

    A campaign created in the console gets ``console-<slug>-promo``. One that
    was converted from a script in this repository keeps the name it always had,
    recorded as ``jobName``, so the conversion edits its existing job in place
    and the run finds the same sent-today state and caption-rotation counters."""
    name = (campaign or {}).get("jobName")
    return name if isinstance(name, str) and JOB_NAME.fullmatch(name) else "console-%s-promo" % slug


def state_directory(slug, campaign=None):
    """Where a campaign's run keeps ``state.json``.

    A converted campaign's state is where its script always kept it, at
    ``runtime/cron-state/<jobName>`` under the state root, so its first run from
    here sees today's sends and never repeats one. A console-created campaign
    keeps the location it was first given, below the Firestore tier: moving it
    now would hide the day's sends from the very next run."""
    name = job_name(slug, campaign)
    if (campaign or {}).get("jobName") == name:
        return Path(group_cleanup.cron_state_dir()) / name
    return Path(config.state_dir()) / "runtime" / "cron-state" / name


def spacing_problem(count, timeout):
    """Why ``count`` groups cannot be reached at full send spacing inside a
    ``timeout``-second job, or ``None`` when they can.

    The engine would still deliver every group by tightening its gaps, and
    tightened gaps are exactly the fixed cadence the spacing exists to avoid. So
    the ceiling is enforced where a campaign is saved, not discovered in a run."""
    import promo_broadcast
    worst = max(count - 1, 0) * promo_broadcast.DEFAULT_SEND_DELAY_MAX + count * SPACING_SEND_SECONDS
    if worst < timeout:
        return None
    return ("%d groups cannot be reached at full spacing within %d seconds; "
            "choose fewer groups or a longer timeout" % (count, timeout))


def validate_definition(slug, data):
    """The rules for a campaign's own fields, whoever is saving them: the
    console's create request, or the one-off conversion of a repository
    campaign (``deploy/migrate-campaigns-to-firestore.py``)."""
    need(isinstance(data.get("name"), str) and 0 < len(data["name"].strip()) <= 120, "enter a campaign name (at most 120 characters)")
    need(type(data.get("enabled")) is bool, "enabled must be boolean")
    captions = data.get("captions")
    need(isinstance(captions, list) and captions and all(isinstance(text, str) and text.strip() for text in captions), "keep at least one nonblank caption")
    schedule = data.get("schedule")
    need(isinstance(schedule, dict) and not set(schedule) - {"time", "timezone", "weekdays"}, "invalid schedule")
    need(isinstance(schedule.get("time"), str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", schedule["time"]), "invalid posting time")
    # Every campaign firing at :00 makes the account's day perfectly periodic.
    need(not schedule["time"].endswith(":00"), "choose a posting time that is not on the hour")
    need(valid_days(schedule.get("weekdays")), "choose campaign weekdays")
    try:
        ZoneInfo(schedule.get("timezone", ""))
    except (ValueError, KeyError, TypeError):
        raise writer.WriteRefused("invalid timezone")
    images = data.get("images")
    need(isinstance(images, list) and 0 < len(images) <= 20, "choose between 1 and 20 pictures")
    filenames, keys = set(), set()
    for index, image in enumerate(images):
        need(isinstance(image, dict) and set(image) == {"filename", "key", "sha256", "bytes", "contentType"}, "invalid picture metadata")
        need(isinstance(image.get("filename"), str) and ad_assets.FILENAME_RE.fullmatch(image["filename"]), "invalid picture filename")
        try:
            ad_assets._entry("%s-%s" % (slug, index + 1), image_entry(slug, image, index))
        except (ValueError, TypeError):
            raise writer.WriteRefused("invalid JPEG/PNG picture metadata (maximum 6 MiB)")
        need(image["key"].startswith("ads/%s/" % slug), "picture belongs to a different campaign")
        need(image["filename"] not in filenames and image["key"] not in keys, "duplicate picture or filename")
        filenames.add(image["filename"])
        keys.add(image["key"])
    timeout = data.get("timeoutSeconds", 7200)
    need(type(timeout) is int and 60 <= timeout <= 7200, "timeout must be 60 to 7200 seconds")
    phone = r"\+[1-9][0-9]{6,14}"
    need(isinstance(data.get("failureAlertTo", ""), str), "invalid failure alert number")
    if data.get("failureAlertTo"):
        need(re.fullmatch(phone, data["failureAlertTo"]), "use an international failure alert number")
    tracking = data.get("tracking", {})
    need(isinstance(tracking, dict) and not set(tracking) - {"prefix", "label", "number", "prefill", "link_line"}, "invalid tracking settings")
    if tracking:
        need(isinstance(tracking.get("prefix"), str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", tracking["prefix"]), "tracking requires a ref prefix")
        need(all(isinstance(value, str) and len(value) <= 1000 for value in tracking.values()), "invalid tracking text")
        if tracking.get("number"):
            need(re.fullmatch(phone, tracking["number"]), "use an international contact number")
    return data


def validate(data):
    need(isinstance(data, dict), "campaign must be an object")
    allowed = {"requestId", "slug", "name", "enabled", "captions", "images", "schedule", "groups",
               "tracking", "comment", "weeklyComment", "timeoutSeconds", "failureAlertTo", "actor"}
    need(not set(data) - allowed, "campaign has unexpected fields")
    need(len(json.dumps(data).encode()) <= MAX_REQUEST, "campaign configuration exceeds 60 KiB")
    need(isinstance(data.get("requestId"), str) and re.fullmatch(r"[a-f0-9-]{36}", data["requestId"]), "invalid request ID")
    need(isinstance(data.get("slug"), str) and SLUG.fullmatch(data["slug"]) and data["slug"] != "new", "invalid or reserved campaign ID")
    validate_definition(data["slug"], data)
    schedule = data["schedule"]
    groups = data.get("groups")
    need(isinstance(groups, list) and 0 < len(groups) <= 100, "choose between 1 and 100 active groups")
    seen = set()
    for group in groups:
        need(isinstance(group, dict) and not set(group) - {"jid", "weekdays", "paused"}, "invalid group assignment")
        jid = group.get("jid")
        need(isinstance(jid, str) and re.fullmatch(r"[0-9-]+@g\.us", jid) and jid not in seen, "invalid or duplicate group ID")
        seen.add(jid)
        need(type(group.get("paused", False)) is bool, "group pause must be boolean")
        if "weekdays" in group:
            need(valid_days(group["weekdays"]) and set(group["weekdays"]) <= set(schedule["weekdays"]), "group weekdays must be a nonempty subset of campaign weekdays")
    problem = spacing_problem(len(groups), data.get("timeoutSeconds", 7200))
    need(problem is None, problem)
    for key in ("comment", "weeklyComment"):
        need(isinstance(data.get(key, ""), str) and len(data.get(key, "")) <= 2000, "notes must be at most 2000 characters")
    need(isinstance(data.get("actor", ""), str) and len(data.get("actor", "")) <= 200, "invalid actor")
    return data


def image_entry(slug, image, index):
    return {"campaign": slug, "filename": image["filename"], "key": image["key"],
            "sha256": image["sha256"], "bytes": image["bytes"], "content_type": image["contentType"],
            "rank": (index + 1) * 10, "enabled": True, "comment": None}


def images_from_assets(entries):
    """A campaign's ``images`` list from its enabled asset-index entries, in
    rotation order: :func:`image_entry` read backwards. The index stays the
    place a picture is uploaded to; this is what a run actually attaches."""
    return [{"filename": entry["filename"], "key": entry["key"], "sha256": entry["sha256"],
             "bytes": entry["bytes"], "contentType": entry["content_type"]}
            for entry in entries if entry.get("enabled", True)]


def image_url(image, env=None):
    origin = ad_assets.base_url(env)
    need(origin is not None, "the bot asset origin is not configured")
    return origin + "/" + image["key"]


def verify_image(image, env=None, opener=None):
    """Bounded download from the configured asset origin, never a supplied URL."""
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(image_url(image, env), headers={"User-Agent": ad_assets.USER_AGENT})
    try:
        with opener(request, timeout=20) as response:
            body = response.read(ad_assets.MAX_BYTES + 1)
    except (OSError, urllib.error.URLError) as ex:
        raise writer.WriteRefused("picture could not be verified; retry the upload", status=503) from ex
    magic = b"\x89PNG\r\n\x1a\n" if image["contentType"] == "image/png" else b"\xff\xd8\xff"
    need(body.startswith(magic) and len(body) == image["bytes"] and hashlib.sha256(body).hexdigest() == image["sha256"], "picture content, size or checksum does not match its upload")


class Store:
    """Firestore REST transaction; retries never reapply a different payload.

    Read and commit use a single snapshot. The native transaction prevents lost
    group edits and duplicate campaign/operation creates across API instances.
    """
    def __init__(self, env=None):
        self.env = os.environ if env is None else env
        creds = config.credentials(self.env)
        need(creds is not None, "Firestore write credentials are unavailable")
        self.project = creds["project_id"]
        self.base = "projects/%s/databases/(default)/documents" % self.project
        self.url = config.API_ROOT + "/" + self.base
        self.token = config.access_token(creds, config.token_cache_path(config.state_dir(self.env)),
                                         opener=urllib.request.urlopen, signer=config.node_signer, now=time.time, sleep=time.sleep)
        self.transaction = None

    def request(self, suffix, body=None, missing=False):
        return config._request(self.url + suffix, data=None if body is None else json.dumps(body).encode(),
                               method="GET" if body is None else "POST", missing_ok=missing,
                               headers=dict(config._auth(self.token), **{"Content-Type": "application/json"}),
                               opener=urllib.request.urlopen, sleep=time.sleep)

    def begin(self):
        self.transaction = self.request(":beginTransaction", {})["transaction"]

    def rollback(self):
        if self.transaction:
            try:
                self.request(":rollback", {"transaction": self.transaction})
            except config.ConfigUnavailable:
                pass
            self.transaction = None

    def query(self):
        return "?" + urllib.parse.urlencode({"transaction": self.transaction}) if self.transaction else ""

    def get(self, collection, doc_id):
        row = self.request("/%s/%s%s" % (collection, urllib.parse.quote(doc_id, safe=""), self.query()), missing=True)
        return config.decode_fields(row.get("fields", {})) if row else None

    def list(self, collection, where=None):
        if where is not None:
            body = {"structuredQuery": {"from": [{"collectionId": collection}], "where": where}}
            if self.transaction:
                body["transaction"] = self.transaction
            return [(config.doc_id_of(row["document"]["name"]), row["document"].get("fields") or {})
                    for row in self.request(":runQuery", body) if "document" in row]
        page, rows = None, []
        while True:
            query = {"pageSize": 1000}
            if self.transaction:
                query["transaction"] = self.transaction
            if page:
                query["pageToken"] = page
            result = self.request("/%s?%s" % (collection, urllib.parse.urlencode(query)))
            rows.extend((config.doc_id_of(row["name"]), row.get("fields", {})) for row in result.get("documents", []))
            page = result.get("nextPageToken")
            if not page:
                return rows

    def commit(self, docs):
        writes = []
        for collection, doc_id, fields, exists in docs:
            write = {"update": {"name": "%s/%s/%s" % (self.base, collection, doc_id), "fields": config.encode_fields(fields)}}
            if exists is not None:
                write["currentDocument"] = {"exists": exists}
            writes.append(write)
        body = {"writes": writes}
        if self.transaction:
            body["transaction"] = self.transaction
        self.request(":commit", body)
        self.transaction = None


def payload_digest(data):
    return hashlib.sha256(json.dumps({key: value for key, value in data.items() if key != "actor"}, sort_keys=True).encode()).hexdigest()


def create(data, store, env=None, verify=verify_image, reconcile=None):
    env = os.environ if env is None else env
    need(promo_config.mode(env)[0] == "firestore", "campaign creation requires live Firestore configuration")
    need(ad_assets.mode(env)[0] == "r2", "campaign creation requires R2 assets")
    validate(data)
    slug = data["slug"]
    digest = payload_digest(data)
    # Check before downloading again on retry, then recheck within transaction.
    operation = store.get("campaign_creations", data["requestId"])
    if operation:
        need(operation.get("digest") == digest, "this request ID already saved different campaign settings; open the saved campaign")
        return finish(slug, store, reconcile)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda image: verify(image, env), data["images"]))
    store.begin()
    try:
        operation = store.get("campaign_creations", data["requestId"])
        if operation:
            need(operation.get("digest") == digest, "creation request has changed")
            store.rollback()
            return finish(slug, store, reconcile)
        settings = store.get("settings", config.SETTINGS_DOC)
        raw = {"settings": [] if settings is None else [(config.SETTINGS_DOC, config.encode_fields(settings))],
               "lines": store.list("lines"), "campaigns": store.list("campaigns"), "groups": []}
        for selection in data["groups"]:
            try:
                group = store.get("groups", selection["jid"])
            except ValueError as exc:
                raise writer.WriteRefused("a selected group cannot be read", status=409) from exc
            if group is not None:
                raw["groups"].append((selection["jid"], config.encode_fields(group)))
        need(slug not in dict(raw["campaigns"]), "a campaign with this ID already exists; choose a different name")
        declared = promo_config.read_manifest(env) or {}
        need(not any(row.get("slug") == slug for row in declared.get("promos", [])), "campaign ID is already declared in the bot")
        campaign = {"kind": "console", "name": data["name"].strip(), "enabled": data["enabled"],
                    "captions": data["captions"], "schedule": data["schedule"], "tracking": data.get("tracking", {}),
                    "comment": data.get("comment", ""), "weekly_comment": data.get("weeklyComment", ""),
                    "timeoutSeconds": data.get("timeoutSeconds", 7200), "failureAlertTo": data.get("failureAlertTo", ""),
                    "images": data["images"], "scheduleStatus": "pending",
                    "rank": writer.next_rank(writer._decoded(raw, "campaigns"))}
        raw["campaigns"].append((slug, config.encode_fields(campaign)))
        group_docs = dict(writer._decoded(raw, "groups"))
        docs = [("campaigns", slug, campaign, False)]
        for selection in data["groups"]:
            jid = selection["jid"]
            group = copy.deepcopy(group_docs.get(jid))
            need(group is not None and promo_membership.is_active(group), "%s is no longer an active configured group" % jid)
            group["promos"] = list(dict.fromkeys(list(group.get("promos") or []) + [slug]))
            if selection.get("paused"):
                group["paused_promos"] = list(dict.fromkeys(list(group.get("paused_promos") or []) + [slug]))
            restriction = selection.get("weekdays", data["schedule"]["weekdays"])
            if set(restriction) != set(range(7)):
                group.setdefault("weekly", {})[slug] = [DAYS[day] for day in sorted(restriction)]
            raw["groups"] = [(key, fields) for key, fields in raw["groups"] if key != jid] + [(jid, config.encode_fields(group))]
            docs.append(("groups", jid, group, True))
        membership, _, undecodable = config.assemble(raw)
        _, report = config.quarantine(membership, undecodable)
        need(not any(selection["jid"] in report.get("quarantined", {}) for selection in data["groups"]), "a group assignment failed the bot validator")
        audit_id, audit = writer.audit_document(data.get("actor"), "campaigns", slug, None, campaign)
        audit["assignments"] = data["groups"]
        docs.extend(("assets", "%s-%d" % (slug, index + 1), image_entry(slug, image, index), False) for index, image in enumerate(data["images"]))
        docs.extend([("audit", audit_id, audit, False), ("campaign_creations", data["requestId"], {"slug": slug, "digest": digest}, False)])
        store.commit(docs)
    finally:
        store.rollback()
    return finish(slug, store, reconcile)


def finish(slug, store, reconcile):
    need(not (store.get("campaigns", slug) or {}).get("deleted"), "this campaign has been deleted")
    try:
        if reconcile is None:
            reconcile_one(slug, store)
        else:
            reconcile(slug, store)
        return {"slug": slug, "scheduling": "ready"}
    except Exception:
        return {"slug": slug, "scheduling": "pending", "warning": "Campaign saved. The gateway schedule is not verified yet; the bot will retry automatically."}


def cron_expression(schedule):
    hour, minute = (int(part) for part in schedule["time"].split(":"))
    days = "*" if len(schedule["weekdays"]) == 7 else ",".join(str((day + 1) % 7) for day in sorted(schedule["weekdays"]))
    return "%s %s * * %s" % (minute, hour, days)


def gateway(arguments):
    environment = dict(os.environ)
    environment.pop("OPENCLAW_HOME", None)
    environment["OPENCLAW_GATEWAY_TOKEN"] = environment.get("GATEWAY_AUTH_TOKEN", "")
    done = subprocess.run(["openclaw", "cron"] + arguments, capture_output=True, text=True, env=environment, timeout=20, check=True)
    return json.loads(done.stdout or "{}") if arguments[0] in ("list", "add") else {}


def reconcile_one(slug, store, call=gateway):
    import fcntl
    directory = Path(config.state_dir()) / "runtime"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "console-campaign-scheduler.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _reconcile_one(slug, store, call)


def _reconcile_one(slug, store, call):
    campaign = store.get("campaigns", slug)
    if not campaign or campaign.get("kind") != "console":
        return
    name = job_name(slug, campaign)
    expression = cron_expression(campaign["schedule"])
    jobs = call(["list", "--all", "--json"]).get("jobs", [])
    matching = [job for job in jobs if job.get("name") == name]
    need(len(matching) <= 1, "duplicate gateway jobs require operator review")
    runner = str(Path(__file__).resolve().parents[1] / "bin" / "run-console-campaign")
    argv = json.dumps(["python3", runner, slug])
    flags = ["--name", name, "--cron", expression, "--tz", campaign["schedule"]["timezone"], "--exact",
             "--session", "isolated", "--command-argv", argv, "--command-cwd", str(Path(runner).parents[1]),
             "--timeout-seconds", str(campaign["timeoutSeconds"]), "--no-deliver"]
    def matches(job):
        return (job.get("enabled") == campaign["enabled"]
                and job.get("schedule", {}).get("expr") == expression
                and job.get("schedule", {}).get("tz") == campaign["schedule"]["timezone"]
                and job.get("payload", {}).get("timeoutSeconds") == campaign["timeoutSeconds"]
                and job.get("payload", {}).get("argv") == json.loads(argv)
                and job.get("payload", {}).get("cwd") == str(Path(runner).parents[1])
                and (not campaign.get("failureAlertTo") or job.get("failureAlert", {}).get("to") == campaign["failureAlertTo"]))
    if matching and matches(matching[0]):
        if campaign.get("scheduleStatus") != "ready":
            mark_status(slug, store, "ready")
        return
    if matching:
        identifier = matching[0]["id"]
        call(["edit", identifier] + flags + (["--enable"] if campaign["enabled"] else ["--disable"]))
    else:
        # Disabled first: no job may race an incomplete creation/reconciliation.
        call(["add"] + flags + ["--disabled", "--json"])
        jobs = call(["list", "--all", "--json"]).get("jobs", [])
        found = [job for job in jobs if job.get("name") == name]
        need(len(found) == 1, "new gateway job was not found uniquely")
        identifier = found[0]["id"]
    if campaign.get("failureAlertTo"):
        call(["edit", identifier, "--failure-alert", "--failure-alert-after", "1", "--failure-alert-channel", "whatsapp",
              "--failure-alert-to", campaign["failureAlertTo"], "--failure-alert-cooldown", "1h"])
    if not matching and campaign["enabled"]:
        call(["edit", identifier, "--enable"])
    verified = [job for job in call(["list", "--all", "--json"]).get("jobs", []) if job.get("name") == name]
    need(len(verified) == 1, "gateway job is not unique")
    job = verified[0]
    need(matches(job), "gateway schedule did not match saved campaign")
    mark_status(slug, store, "ready")


def mark_status(slug, store, status):
    # A field mask avoids overwriting caption/switch edits during reconciliation.
    import console_config
    current = store.get("campaigns", slug)
    if not current or current.get("scheduleStatus") == status:
        return False
    # Invalidate only when reconciliation actually writes, not every minute.
    with console_config.mutation():
        store.request(":commit", {"writes": [{"update": {"name": "%s/campaigns/%s" % (store.base, slug),
                       "fields": config.encode_fields({"scheduleStatus": status})}, "updateMask": {"fieldPaths": ["scheduleStatus"]},
                       "currentDocument": {"exists": True}}]})
    return True


def set_images(project, token, slug, images, *, opener=None, sleep=None):
    """Replace one campaign's ``images`` and nothing else.

    A field mask for the reason :func:`mark_status` uses one: the caller read
    the document a moment ago, and a whole-document write would undo a caption
    or switch edit, or a schedule status, that landed in between."""
    base = "projects/%s/databases/(default)/documents" % project
    body = {"writes": [{"update": {"name": "%s/campaigns/%s" % (base, slug),
                                   "fields": config.encode_fields({"images": images})},
                        "updateMask": {"fieldPaths": ["images"]},
                        "currentDocument": {"exists": True}}]}
    config._request("%s/%s:commit" % (config.API_ROOT, base), data=json.dumps(body).encode(),
                    method="POST", headers=dict(config._auth(token), **{"Content-Type": "application/json"}),
                    opener=opener or urllib.request.urlopen, sleep=sleep or time.sleep)


def reconcile_all(store):
    failures = 0
    for slug, fields in store.list("campaigns", where={"fieldFilter": {
            "field": {"fieldPath": "kind"}, "op": "EQUAL", "value": {"stringValue": "console"}}}):
        if config.decode_fields(fields).get("kind") == "console":
            try:
                reconcile_one(slug, store)
            except Exception as ex:
                failures += 1
                mark_status(slug, store, "pending")
                sys.stderr.write("[console-campaign] schedule pending for %s: %s\n" % (slug, type(ex).__name__))
    return failures


def schedule_occurrence(campaign, day):
    """The local and displayed occurrence on a Harare Schedule date."""
    display = ZoneInfo("Africa/Harare")
    zone = ZoneInfo(campaign["schedule"]["timezone"])
    hour, minute = map(int, campaign["schedule"]["time"].split(":"))
    for delta in (-1, 0, 1):
        local_date = day + dt.timedelta(days=delta)
        if local_date.weekday() not in campaign["schedule"]["weekdays"]:
            continue
        local = dt.datetime.combine(local_date, dt.time(hour, minute), zone)
        shown = local.astimezone(display)
        if shown.date() == day:
            return local_date, shown.hour * 60 + shown.minute
    return None


def schedule_minutes(campaign, day):
    occurrence = schedule_occurrence(campaign, day)
    return occurrence[1] if occurrence else None


def definitions(membership):
    return {slug: data for slug, data in (membership.get("promos") or {}).items() if data.get("kind") == "console"}


def records(membership, day, switches=None, only_slug=None):
    result = []
    for slug, campaign in definitions(membership).items():
        if only_slug is not None and slug != only_slug:
            continue
        daily, weekly = promo_membership.groups_for_promo(membership, slug)
        accounts = {}
        for jid in list(daily) + list(weekly):
            line = promo_membership.account_of(membership["groups"][jid])
            accounts[line] = accounts.get(line, 0) + 1
        hour, minute = map(int, campaign["schedule"]["time"].split(":"))
        result.append({"slug": slug, "name": campaign["name"], "enabled": bool((switches or {}).get(slug)) and campaign.get("scheduleStatus") == "ready",
                       "storeEnabled": bool((switches or {}).get(slug)), "cronEnabled": campaign.get("scheduleStatus") == "ready",
                       "cron": cron_expression(campaign["schedule"]), "hour": hour, "minute": minute,
                       "scheduleMinutes": schedule_minutes(campaign, day), "timezone": campaign["schedule"]["timezone"],
                       "installer": "", "script": "", "failureAlertTo": campaign.get("failureAlertTo", ""), "legacyNames": [],
                       "timeoutSeconds": campaign["timeoutSeconds"], "captions": campaign["captions"], "tracking": campaign.get("tracking", {}),
                       "images": [image["filename"] for image in campaign["images"]],
                       "assets": [{"filename": image["filename"], "url": image_url(image),
                                   "sha256": image["sha256"], "bytes": image["bytes"],
                                   "contentType": image["contentType"], "rank": (index + 1) * 10}
                                  for index, image in enumerate(campaign["images"])],
                       "targetCount": len(daily) + len(weekly), "weeklyCount": len(weekly), "accounts": accounts,
                       "source": "console", "jobName": job_name(slug, campaign),
                       "scheduleStatus": campaign.get("scheduleStatus", "pending"), "error": None})
    return result


def run_campaign(slug, argv=None, now=None, resolve=None, broadcast=None, ensure=None):
    """Reuse the complete broadcast engine, with one config read and day."""
    import fcntl
    import promo_broadcast
    need(isinstance(slug, str) and SLUG.fullmatch(slug), "invalid campaign ID")
    # The resolver removes one job-name suffix. Supply it explicitly so an ID
    # that itself ends in '-promo' keeps its literal identity and targeting.
    source = (resolve or promo_config.resolve)(slug + "-promo")
    campaign = definitions(source.get("membership") or {}).get(slug)
    need(source.get("mode") == "firestore" and campaign is not None, "console campaign requires Firestore configuration")
    dry = "--dry-run" in (argv or []) or "--refs" in (argv or [])
    if not dry and (source.get("enabled") is not True or campaign.get("scheduleStatus") != "ready"):
        return 0
    now = now or dt.datetime.now(dt.timezone.utc)
    day = now.astimezone(ZoneInfo(campaign["schedule"]["timezone"])).date()
    if not dry and day.weekday() not in campaign["schedule"]["weekdays"]:
        return 0
    # Keep strict all-picture validation for data-only campaigns: there are no
    # baked files to fall back to, and a missing asset must never change rotation.
    paths = [(ensure or ad_assets.ensure)(image_entry(slug, image, index))
             for index, image in enumerate(campaign["images"])]
    state = state_directory(slug, campaign)
    state.mkdir(parents=True, exist_ok=True)
    with (state / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return (broadcast or promo_broadcast.broadcast)(text=campaign["captions"], image=paths,
                    targets_file=str(state / "targets.json"), state_file=str(state / "state.json"),
                    tracking=campaign.get("tracking") or None, promo_name=slug + "-promo", argv=argv or [],
                    cron_timeout=campaign["timeoutSeconds"], run_day=day, cap_day=now.astimezone(ZoneInfo("Africa/Harare")).date(), resolve_config=lambda *args, **kwargs: source)
