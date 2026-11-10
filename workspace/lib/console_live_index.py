"""Durable, display-only Firestore index. Never imported by writers or sends.

The single API process owns the leased stream. Readers open independent SQLite
snapshots. Documents and resume positions publish together, never mid-bootstrap.
"""
import contextlib
import datetime
import hashlib
import json
import os
import sqlite3
import threading
import time

import operator_config as config

SCHEMA = 2
COLLECTIONS = ("settings", "lines", "campaigns", "groups", "campaign_deletions")
LEASE_SECONDS = 90


class NotReady(Exception):
    pass


def identity(env):
    creds = config.credentials(env)
    if not creds:
        raise config.ConfigUnavailable("no service account configured")
    return hashlib.sha256(json.dumps([SCHEMA, config.API_ROOT, creds], sort_keys=True).encode()).hexdigest()


class Index:
    def __init__(self, env=None, *, clock=time.time, stream=None):
        self.env = dict(os.environ if env is None else env)
        self.clock = clock
        self.identity = identity(self.env)
        self.path = os.path.join(config.state_dir(self.env), "console-live.sqlite")
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.lease_until = 0
        self.stopped = False
        self.thread = None
        self.cancel = None
        self.stream = stream or firestore_stream
        self.error = None
        self.connected = False
        self.counters = {"syncDocuments": 0, "rebuilds": 0, "activeStreams": 0,
                         "indexedRequests": 0, "returnedRows": 0}
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA:
                db.execute("DROP TABLE IF EXISTS docs")
                db.execute("DROP TABLE IF EXISTS meta")
            db.execute("PRAGMA user_version=%d" % SCHEMA)
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT,
                generation INTEGER, revision INTEGER, read_time TEXT, tokens TEXT);
              CREATE TABLE IF NOT EXISTS docs (generation INTEGER, collection TEXT, id TEXT, body TEXT,
                PRIMARY KEY(generation, collection, id));
            """)
            row = db.execute("SELECT identity FROM meta WHERE id=1").fetchone()
            if row is None or row[0] != self.identity:
                db.execute("DELETE FROM docs")
                db.execute("DELETE FROM meta")
                db.execute("INSERT INTO meta VALUES(1,?,0,0,NULL,'{}')", (self.identity,))
            db.execute("DELETE FROM docs WHERE generation != (SELECT generation FROM meta WHERE id=1)")
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def checkpoint(self):
        with self.db() as db:
            generation, revision, stamp, tokens = db.execute(
                "SELECT generation,revision,read_time,tokens FROM meta WHERE id=1").fetchone()
        return {"generation": generation, "revision": revision, "readTime": stamp, "tokens": json.loads(tokens)}

    def touch(self):
        with self.condition:
            self.lease_until = self.clock() + LEASE_SECONDS
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="console-index", daemon=True)
                self.thread.start()
            self.condition.notify_all()

    def status(self):
        point = self.checkpoint()
        stamp = point["readTime"]
        lag = max(0, self.clock() - datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()) if stamp else None
        return {"revision": str(point["revision"]), "syncedAt": stamp, "lagSeconds": lag,
                "stale": not self.connected or bool(self.error),
                "state": "syncing" if not point["readTime"] else "stale" if not self.connected or self.error else "live"}

    def documents(self, collection):
        with self.db() as db:
            return [(name, config.decode_fields(json.loads(body))) for name, body in db.execute(
                "SELECT id,body FROM docs WHERE generation=(SELECT generation FROM meta WHERE id=1) AND collection=? ORDER BY id", (collection,))]

    def observation_membership(self, include_ids=False):
        """Telemetry needs account numbers; discovery additionally needs IDs only."""
        raw = {collection: [] for collection in COLLECTIONS}
        with self.db() as db:
            db.execute("BEGIN")
            generation, stamp = db.execute("SELECT generation,read_time FROM meta WHERE id=1").fetchone()
            if not stamp:
                raise NotReady("The display index is synchronizing.")
            for collection, name, body in db.execute(
                    "SELECT collection,id,body FROM docs WHERE generation=? AND collection IN ('settings','lines')", (generation,)):
                raw[collection].append((name, json.loads(body)))
            membership = config.assemble(raw)[0]
            if include_ids:
                membership["groups"] = {name: {} for (name,) in db.execute(
                    "SELECT id FROM docs WHERE generation=? AND collection='groups'", (generation,))}
        return membership

    def snapshot(self):
        with self.db() as db:
            db.execute("BEGIN")
            generation, revision, stamp = db.execute(
                "SELECT generation,revision,read_time FROM meta WHERE id=1").fetchone()
            if not stamp:
                raise NotReady("The display index is synchronizing. Try again shortly.")
            raw = {collection: [] for collection in COLLECTIONS}
            for collection, name, body in db.execute(
                    "SELECT collection,id,body FROM docs WHERE generation=? ORDER BY collection,id", (generation,)):
                raw[collection].append((name, json.loads(body)))
        import campaign_deletion
        deleted = sorted(campaign_deletion.deleted_slugs(raw))
        membership, enabled, undecodable = config.assemble(raw)
        membership, report = config.quarantine(membership, undecodable)
        enabled, report["campaigns"] = config.campaign_switches(enabled)
        return {"source": "snapshot", "membership": membership, "enabled": enabled,
                "report": report, "fetchedAt": stamp, "degraded": None, "deleted": deleted}, str(revision)

    def publish(self, changes, read_time, tokens, *, reset=False):
        """Called only at a global consistent stream boundary."""
        with self.lock, self.db() as db:
            generation, revision = db.execute("SELECT generation,revision FROM meta WHERE id=1").fetchone()
            if reset:
                generation += 1
                self.counters["rebuilds"] += 1
            changed = reset
            for collection, name, fields in changes:
                if collection not in COLLECTIONS:
                    raise ValueError("unexpected index collection")
                previous = db.execute("SELECT body FROM docs WHERE generation=? AND collection=? AND id=?",
                                      (generation, collection, name)).fetchone()
                body = json.dumps(fields, sort_keys=True) if fields is not None else None
                if (previous[0] if previous else None) == body:
                    continue
                changed = True
                if fields is None:
                    db.execute("DELETE FROM docs WHERE generation=? AND collection=? AND id=?", (generation, collection, name))
                else:
                    db.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?)", (generation, collection, name, body))
            # Validate shared configuration before making this generation visible.
            raw = {collection: [] for collection in COLLECTIONS}
            for collection, name, body in db.execute("SELECT collection,id,body FROM docs WHERE generation=?", (generation,)):
                raw[collection].append((name, json.loads(body)))
            membership, enabled, undecodable = config.assemble(raw)
            config.quarantine(membership, undecodable)
            config.campaign_switches(enabled)
            db.execute("UPDATE meta SET generation=?,revision=?,read_time=?,tokens=? WHERE id=1",
                       (generation, revision + int(changed), read_time, json.dumps(tokens)))
            db.execute("DELETE FROM docs WHERE generation != ?", (generation,))
        self.error = None

    def _run(self):
        delay = 1
        while True:
            with self.condition:
                while not self.stopped and self.clock() >= self.lease_until:
                    self.connected = False
                    self.condition.wait(1)
                if self.stopped:
                    return
            try:
                self.counters["activeStreams"] = 1
                self.stream(self)
                delay = 1
            except Exception as exc:
                # Curated status only. Exception text may contain credential context.
                self.error = type(exc).__name__
                self.connected = False
                with self.condition:
                    self.condition.wait(delay)
                delay = min(delay * 2, 300)
            finally:
                self.counters["activeStreams"] = 0

    def close(self):
        with self.condition:
            self.stopped = True
            if self.cancel:
                self.cancel()
            self.condition.notify_all()
        if self.thread:
            self.thread.join(5)


def firestore_stream(index):
    """Public Listen RPC, with durable resume tokens and an idle cancellation timer."""
    import base64
    from google.cloud.firestore_v1.services.firestore import FirestoreClient
    from google.cloud.firestore_v1.types import ListenRequest, Target
    from google.oauth2 import service_account
    from google.protobuf.json_format import MessageToDict
    from google.protobuf.timestamp_pb2 import Timestamp
    from google.api_core.exceptions import InvalidArgument, FailedPrecondition

    creds = config.credentials(index.env)
    client = FirestoreClient(credentials=service_account.Credentials.from_service_account_info(creds))
    database = "projects/%s/databases/(default)" % creds["project_id"]
    point = index.checkpoint()
    bootstrap = not point["readTime"] or point["tokens"].get("reset", False)
    tokens = {} if bootstrap else point["tokens"].copy()
    requests = []
    for number, collection in enumerate(COLLECTIONS, 1):
        target = Target(target_id=number, query={"parent": database + "/documents",
                        "structured_query": {"from_": [{"collection_id": collection}]}})
        if not bootstrap:
            if str(number) in tokens:
                target.resume_token = base64.b64decode(tokens[str(number)])
            else:
                stamp = Timestamp()
                stamp.FromJsonString(point["readTime"])
                target.read_time = stamp
        requests.append(ListenRequest(database=database, add_target=target))
    # Keep the bidirectional request stream open after adding the targets.
    done = threading.Event()
    def request_iter():
        yield from requests
        done.wait()
    response = None
    def cancel():
        if response is not None:
            response.cancel()
        else:
            client.transport.close()
    index.cancel = cancel
    def monitor():
        while not done.wait(0.5):
            if index.stopped or index.clock() >= index.lease_until:
                cancel()
                return
    threading.Thread(target=monitor, daemon=True).start()
    pending = []
    known = {collection: set() if bootstrap else {name for name, _ in index.documents(collection)} for collection in COLLECTIONS}
    current = set()
    try:
        response = client.listen(requests=request_iter(), retry=None,
                                 metadata=(("google-cloud-resource-prefix", database),))
        for change in response:
            kind = change._pb.WhichOneof("response_type")
            if kind == "document_change":
                doc = change.document_change.document
                collection, name = doc.name.rsplit("/", 2)[-2:]
                known[collection].add(name)
                pending.append((collection, name, MessageToDict(doc._pb).get("fields", {})))
                index.counters["syncDocuments"] += 1
            elif kind in ("document_delete", "document_remove"):
                # An unfiltered collection target has no legitimate remove-only
                # event except a reset; a reset rebuilds the entire generation.
                document = getattr(change, kind).document
                collection, name = document.rsplit("/", 2)[-2:]
                known[collection].discard(name)
                pending.append((collection, name, None))
            elif kind == "filter":
                number = change.filter.target_id
                if not 1 <= number <= len(COLLECTIONS) or len(known[COLLECTIONS[number - 1]]) != change.filter.count:
                    # Only a mismatch requires a rebuild; normal existence
                    # filters must never trigger a repeat collection bootstrap.
                    raise ResetRequired()
            elif kind == "target_change":
                target = change.target_change
                ids = list(target.target_ids) or list(range(1, len(COLLECTIONS) + 1))
                state = int(target.target_change_type)
                if state == 2:
                    raise config.ConfigUnavailable("index target refused")
                if state == 4:
                    raise ResetRequired()
                if state == 3:
                    current.update(ids)
                if target.resume_token:
                    for number in ids:
                        tokens[str(number)] = base64.b64encode(target.resume_token).decode()
                if target.read_time and not target.target_ids and current == set(range(1, len(COLLECTIONS) + 1)):
                    stamp = target._pb.read_time.ToJsonString()
                    index.publish(pending, stamp, tokens, reset=bootstrap)
                    pending = []
                    bootstrap = False
                    index.connected = True
    except (ResetRequired, InvalidArgument, FailedPrecondition):
        # Forget resume positions only. Keep the published generation readable
        # until a replacement has reached CURRENT for every collection.
        with index.db() as db:
            db.execute("UPDATE meta SET tokens=? WHERE id=1", (json.dumps({"reset": True}),))
        raise
    except Exception:
        if not index.stopped and index.clock() < index.lease_until:
            raise
    finally:
        done.set()
        cancel()
        index.cancel = None
        index.connected = False
        client.transport.close()


class ResetRequired(Exception):
    pass
