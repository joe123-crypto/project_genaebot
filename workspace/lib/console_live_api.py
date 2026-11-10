"""Small authenticated display endpoints over the shared local index."""
import base64
import datetime
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import console_live_index
import group_discovery
import group_metrics

PAGE_SIZE = 10


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def title(slug):
    return " ".join(part.upper() if part in ("cv", "it", "ict", "ai", "r2") else part.capitalize() for part in slug.split("-"))


def cursor_encode(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def cursor_decode(value):
    try:
        if len(value) > 4096:
            raise ValueError()
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except Exception as exc:
        raise ValueError("Invalid page cursor.") from exc


class LiveViews:
    def __init__(self, bridge_path, env=None, *, index=None):
        self.env = dict(os.environ if env is None else env)
        self.index = index or console_live_index.Index(self.env)
        loader = importlib.machinery.SourceFileLoader("console_live_read", str(bridge_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.bridge = importlib.util.module_from_spec(spec)
        loader.exec_module(self.bridge)
        self.lock = threading.RLock()
        self.writes_active = 0
        self.write_fence = None
        self.cache = None
        self.cache_at = 0
        self.sql = sqlite3.connect(":memory:", check_same_thread=False)
        self.sql.execute("CREATE TABLE rows(kind TEXT,id TEXT,position INTEGER,body TEXT,PRIMARY KEY(kind,id))")

    def begin_write(self):
        with self.lock:
            self.writes_active += 1

    def fence(self):
        with self.lock:
            self.writes_active = max(0, self.writes_active - 1)
            self.write_fence = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            self.cache = None
            self.index.request_boundary()

    def close(self):
        self.index.close()
        with self.lock:
            self.sql.close()

    def _model(self):
        if self.writes_active:
            raise console_live_index.NotReady("Waiting for the active write.")
        config, revision = self.index.snapshot()
        if self.write_fence:
            if datetime.datetime.fromisoformat(config["fetchedAt"].replace("Z", "+00:00")) < datetime.datetime.fromisoformat(self.write_fence.replace("Z", "+00:00")):
                raise console_live_index.NotReady("Waiting for the committed changes to reach the display index.")
            self.write_fence = None
            config, revision = self.index.snapshot()
        now = time.time()
        day = datetime.datetime.now(ZoneInfo("Africa/Harare")).date()
        signature = (revision, str(day))
        # Local histories may change cap allowances without a config write.
        if self.cache and self.cache[0] == signature and now - self.cache_at < 30:
            return self.cache[1]
        bridge = self.bridge
        bridge._RESOLVED.clear()
        bridge._RESOLVED["config"] = bridge.promo_config.resolve(
            None, env=self.env, membership_path=bridge.MEMBERSHIP,
            config_loader=lambda *args, **kwargs: config, err=io.StringIO())
        groups = bridge.group_rows(day)
        campaigns = bridge.campaign_records(day, include_assets=False)
        # No image catalogue reads. List copy is a count, not downloaded captions.
        for row in campaigns:
            row["captionCount"] = len(row.get("captions", []))
            row.pop("captions", None)
            row.pop("images", None)
            row.pop("assets", None)
            row.pop("script", None)
        def band(row):
            return 0 if row["status"] != "active" else 1 if row["promos"] else 2
        groups.sort(key=lambda row: (band(row), row["jid"]))
        campaigns.sort(key=lambda row: (row["hour"] if row["hour"] is not None else 99, row["minute"] or 0, row["slug"]))
        self.sql.execute("DELETE FROM rows")
        for kind, rows, key in (("groups", groups, "jid"), ("campaigns", campaigns, "slug")):
            self.sql.executemany("INSERT INTO rows VALUES(?,?,?,?)", [
                (kind, row[key], i, json.dumps(row)) for i, row in enumerate(rows)])
        self.sql.commit()
        model = {"date": str(day), "groups": groups, "campaigns": campaigns, "config": config,
                 "context": {"accounts": bridge.accounts(config["membership"]),
                    "capSettings": bridge.cap_settings(config["membership"]),
                    "writes": bridge.write_capabilities(), "actions": bridge.action_capabilities()},
                 "disabledPromos": sorted(bridge.disabled_promos(bridge.manifest(), day))}
        self.cache = (signature, model)
        self.cache_at = now
        return model

    def handle(self, route, query):
        values = parse_qs(query, keep_blank_values=True)
        if any(len(v) != 1 for v in values.values()):
            raise ValueError("Repeated query parameters are not supported.")
        params = {k: v[0] for k, v in values.items()}
        allowed = {"q", "status", "line", "campaign", "tier", "cadence", "slot", "cursor", "anchor", "since", "ids", "slug"}
        if set(params) - allowed or len(params.get("q", "")) > 512:
            raise ValueError("Invalid display query.")
        if route not in ("metrics", "discoveries"):
            self.index.touch()
        elif self.index.thread is not None:
            with self.index.condition:
                self.index.lease_until = self.index.clock() + console_live_index.LEASE_SECONDS
        self.index.counters["indexedRequests"] += 1
        # Never hold the projection lock while waiting on the network. Local
        # telemetry and other requests must remain usable during an outage.
        if route not in ("metrics", "discoveries", "status"):
            with self.lock:
                boundary = self.write_fence if not self.writes_active else None
            if boundary:
                self.index.await_boundary(boundary)
        with self.lock:
            if route == "metrics":
                return self.metrics(params)
            if route == "status":
                return {"sync": self.index.status(), "counters": dict(self.index.counters)}
            if route == "discoveries":
                membership = self.index.observation_membership(include_ids=True)
                result = group_discovery.read(membership, self.env)
                rows = result.get("discoveredGroups", [])
                rows.sort(key=lambda row: (row["name"] or "", row["jid"]))
                return self.page("discoveries", rows, params, digest(result), self.index.status(),
                                 {"discovery": result.get("groupDiscovery")})
            try:
                model = self._model()
            except console_live_index.NotReady:
                if params.get("since"):
                    sync = dict(self.index.status(), stale=True, state="awaiting-commit")
                    return {"sync": sync, "revision": params["since"], "unchanged": True}
                raise
            sync = self.index.status()
            if route == "navigation":
                return {"sync": sync, "campaignTotal": len(model["campaigns"]),
                        "groupsNeedingReview": sum(row["status"] != "active" for row in model["groups"])}
            if route == "settings":
                return dict(model["context"], sync=sync)
            if route == "lines":
                off = set(model["disabledPromos"])
                lines = []
                for account in model["context"]["accounts"]:
                    mine = [row for row in model["groups"] if row["account"] == account["id"]]
                    active = [row for row in mine if row["status"] == "active"]
                    counts = {}
                    for group in active:
                        for slug in group["promos"]:
                            counts[slug] = counts.get(slug, 0) + 1
                    promos = [{"slug": slug, "count": count, "off": slug in off} for slug, count in counts.items()]
                    promos.sort(key=lambda row: -row["count"])
                    lines.append(dict(account, groupCount=len(mine), advertisingCount=len(active),
                                      campaignCount=sum(not row["off"] for row in promos), promos=promos))
                return {"lines": lines, "writes": model["context"]["writes"], "sync": sync}
            if route == "schedule":
                slots = [{"slug": row["slug"], "name": row["name"],
                          "minutes": row.get("scheduleMinutes", (row["hour"] or 0) * 60 + (row["minute"] or 0)),
                          "targetCount": row["targetCount"], "accounts": row["accounts"],
                          **({"scheduleEdit": row["scheduleEdit"]} if row.get("scheduleEdit") else {})}
                         for row in model["campaigns"] if row["enabled"] and row["hour"] is not None and row.get("scheduleMinutes", 0) is not None]
                for slot in slots:
                    slot["time"] = "%02d:%02d" % divmod(slot["minutes"], 60)
                payload = {"date": model["date"], "timezone": "Africa/Harare", "slots": slots,
                           "writes": model["context"]["writes"]}
                revision = digest(payload)
                return {"sync": sync, "revision": revision, **({"unchanged": True} if params.get("since") == revision else payload)}
            if route == "group":
                jid = params.get("ids", "")
                if not group_discovery.GROUP.fullmatch(jid):
                    raise ValueError("Invalid group.")
                return {"group": next((row for row in model["groups"] if row["jid"] == jid), None), "sync": sync}
            if route in ("campaign", "captions", "detail", "preview"):
                slug = params.get("slug", "")
                if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
                    raise ValueError("Invalid campaign.")
                row = next((row for row in model["campaigns"] if row["slug"] == slug), None)
                if row is None:
                    return {"campaign": None, "sync": sync}
                if route == "campaign":
                    revision = digest([row, model["context"]])
                    return {"sync": sync, "revision": revision, **({"unchanged": True} if params.get("since") == revision
                            else {"campaign": row, "context": model["context"]})}
                day = datetime.date.fromisoformat(model["date"])
                if route == "captions":
                    records = self.bridge.campaign_records(day, only_slug=slug, include_assets=False)
                    campaign = records[0] if records else None
                    return {"campaign": {key: campaign[key] for key in ("slug", "name", "source")} if campaign else None,
                            "captions": campaign["captions"] if campaign else [], "writes": model["context"]["writes"], "sync": sync}
                if route == "detail":
                    records = self.bridge.campaign_records(day, only_slug=slug, include_assets=True)
                    return {"campaign": records[0] if records else None, "adAssets": self.bridge.ad_assets_status(), "sync": sync}
                preview = self.bridge.preview(slug, day)
                rows = preview.pop("groups")
                return self.page("preview", rows, params, digest(preview | {"groups": rows}), sync,
                                 {"variantCount": len(preview["captions"]), "context": model["context"]})
            if route in ("groups", "campaigns"):
                self.sql.create_function("matches", 1, lambda body: int(matches(route, json.loads(body), params)))
                rows = [json.loads(body) for (body,) in self.sql.execute(
                    "SELECT body FROM rows WHERE kind=? AND matches(body) ORDER BY position", (route,))]
                revision = digest([model["date"], rows, model["context"], self.deletions() if route == "campaigns" else model["disabledPromos"]])
                facets = {"lines": [a["id"] for a in model["context"]["accounts"]],
                          "campaigns": [{"slug": c["slug"], "name": c["name"], "enabled": c["enabled"]} for c in model["campaigns"]],
                          "tiers": list((model["context"].get("capSettings") or {}).get("bands", {"busy": [], "normal": [], "quiet": []}))}
                return self.page(route, rows, params, revision, sync,
                                 {"context": model["context"], "facets": facets, "disabledPromos": model["disabledPromos"],
                                  "deletions": self.deletions() if route == "campaigns" else []})
            raise ValueError("Unknown live view.")

    def deletions(self):
        # Cleanup receipts are synchronized alongside config, never re-queried
        # by list polling. Older bot checkouts simply have no receipts.
        return [{"slug": slug, "name": row.get("name", slug), "cleanup": row.get("cleanup", "pending"),
                 "detail": row.get("detail")} for slug, row in self.index.documents("campaign_deletions")]

    def page(self, kind, rows, params, revision, sync, extra):
        key = "slug" if kind == "campaigns" else "jid"
        signature = digest({k: v for k, v in params.items() if k not in ("cursor", "anchor", "since")})
        offset = 0
        if params.get("cursor"):
            point = cursor_decode(params["cursor"])
            if not isinstance(point, dict) or point.get("kind") != kind or point.get("query") != signature:
                raise ValueError("Page cursor belongs to another query.")
            if point.get("revision") != revision:
                return {"reset": True, "sync": sync, "revision": revision}
            offset = point.get("offset")
            if type(offset) is not int or offset < 0:
                raise ValueError("Invalid page cursor.")
        elif params.get("anchor"):
            offset = next((i for i, row in enumerate(rows) if row[key] == params["anchor"]), 0)
        page = rows[offset:offset + PAGE_SIZE]
        if kind == "groups":
            page = [dict(row, note=None) for row in page]
        next_cursor = cursor_encode({"kind": kind, "query": signature, "revision": revision, "offset": offset + PAGE_SIZE}) if offset + PAGE_SIZE < len(rows) else None
        self.index.counters["returnedRows"] += len(page) if params.get("since") != revision else 0
        payload = {"sync": sync, "revision": revision}
        if params.get("since") == revision:
            return dict(payload, unchanged=True)
        return dict(payload, rows=page, nextCursor=next_cursor, total=len(rows), **extra)

    def metrics(self, params):
        ids = list(dict.fromkeys(params.get("ids", "").split(","))) if params.get("ids") else []
        if len(ids) > 100 or any(not group_discovery.GROUP.fullmatch(jid) for jid in ids):
            raise ValueError("Invalid telemetry group selection.")
        try:
            membership = self.index.observation_membership()
        except console_live_index.NotReady:
            membership = {"defaults": {"accounts": {}}}
        payload = {"groups": [{"jid": jid} for jid in ids]}
        group_metrics.attach(payload, membership, self.env, group_ids=ids)
        return {"rows": payload["groups"], "sync": self.index.status()}


def matches(kind, row, params):
    q = params.get("q", "").strip().lower()
    if kind == "groups":
        haystack = " ".join([row.get("name") or "Unnamed group", row["jid"], row["jid"].removesuffix("@g.us")])
        if params.get("ids") and row["jid"] not in params["ids"].split(","):
            return False
        if q not in haystack.lower():
            return False
        status = params.get("status", "all")
        active = row["status"] == "active"
        if status == "advertising" and (not active or not row["promos"]) or status == "needs-decision" and active:
            return False
        if status == "no-campaign" and (not active or row["promos"]):
            return False
        if status not in ("all", "advertising", "needs-decision", "no-campaign") and row["status"] != status:
            return False
        return (not params.get("line") or row["account"] == params["line"]) and (not params.get("tier") or row["activity"] == params["tier"]) and (not params.get("campaign") or params["campaign"] in row["promos"])
    haystack = " ".join([title(row["slug"]), row["slug"], row["name"] or "", (row.get("tracking") or {}).get("prefix", "")])
    running = row["enabled"] and not row.get("error")
    status = params.get("status", "all")
    if q not in haystack.lower() or status == "running" and not running or status == "off" and row["enabled"] or status == "failing" and not row.get("error") or status == "no-groups" and (not running or row["targetCount"]):
        return False
    hour = row.get("hour")
    slot = "unscheduled" if hour is None else "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
    return (not params.get("line") or params["line"] in row["accounts"]) and (not params.get("cadence") or (row["weeklyCount"] > 0) == (params["cadence"] == "weekly")) and (not params.get("slot") or params["slot"] == slot)
