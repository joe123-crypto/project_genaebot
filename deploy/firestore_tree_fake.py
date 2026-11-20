"""An in-memory Firestore REST endpoint that knows about document trees.

The older fakes key documents by collection alone, which cannot tell
``groups/<jid>`` from ``workspaces/<id>/groups/<jid>``. This one keys every
document by its full path below ``documents/`` and answers the calls the bot
and the workspace tools make, with Firestore's own addressing rules:

* lists and gets address a collection or document path;
* ``:runQuery``, ``:runAggregationQuery`` and ``:listCollectionIds`` hang off a
  parent (the database root or a document);
* ``:commit``, ``:beginTransaction`` and ``:rollback`` are accepted only on the
  database path. Sent anywhere else, Firestore answers 404 -- and so does this,
  which is the point: a workspace bot must not address them to its workspace.

Test-only; imported by the deploy/ and workspace/lib tests.
"""

import io
import json
import urllib.error
import urllib.parse

TOKEN_URL = "https://oauth2.googleapis.com/token"
DATABASE_CALLS = (":commit", ":beginTransaction", ":rollback")


class Response(object):
    def __init__(self, payload):
        self._body = io.BytesIO(json.dumps(payload).encode())

    def read(self):
        return self._body.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _decode(value):
    kind, inner = next(iter(value.items()))
    if kind == "integerValue":
        return int(inner)
    if kind == "arrayValue":
        return [_decode(v) for v in inner.get("values", [])]
    if kind == "mapValue":
        return dict((k, _decode(v)) for k, v in inner.get("fields", {}).items())
    if kind == "nullValue":
        return None
    return inner


class FakeTree(object):
    def __init__(self, project, docs=None):
        self.project = project
        self.prefix = "/v1/projects/%s/databases/(default)/documents" % project
        self.name_prefix = "projects/%s/databases/(default)/documents/" % project
        self.docs = dict(docs or {})      # "groups/<id>" or "workspaces/w/groups/<id>" -> fields
        self.requests = []                # (method, path below documents, or the call)

    # ---------------------------------------------------------------- helpers
    def put(self, path, fields):
        self.docs[path] = fields

    def children(self, parent, collection):
        base = (parent + "/" if parent else "") + collection + "/"
        return sorted((path[len(base):], fields) for path, fields in self.docs.items()
                      if path.startswith(base) and "/" not in path[len(base):])

    def tree(self, parent):
        """``{collection: {doc_id: fields}}`` directly below ``parent``."""
        out = {}
        base = parent + "/" if parent else ""
        for path, fields in self.docs.items():
            if not path.startswith(base):
                continue
            rest = path[len(base):].split("/")
            if len(rest) == 2:
                out.setdefault(rest[0], {})[rest[1]] = fields
        return out

    # ------------------------------------------------------------------ HTTP
    def __call__(self, request, timeout=None):
        url = request.full_url
        if url == TOKEN_URL:
            return Response({"access_token": "tok", "expires_in": 3600})
        parts = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parts.path)
        if not path.startswith(self.prefix):
            raise urllib.error.HTTPError(url, 404, "wrong project", {}, io.BytesIO())
        rest = path[len(self.prefix):]
        body = json.loads(request.data) if request.data else None
        method = request.get_method()
        # Calls: "<parent>:<verb>" where parent is "" (database) or "/a/b".
        if ":" in rest.rsplit("/", 1)[-1]:
            parent, verb = rest.rsplit(":", 1)
            verb = ":" + verb
            parent = parent.lstrip("/")
            self.requests.append((method, verb, parent))
            if verb in DATABASE_CALLS and parent:
                raise urllib.error.HTTPError(url, 404, "%s is a database call" % verb, {}, io.BytesIO())
            return Response(getattr(self, "_" + verb[1:])(parent, body))
        segments = [s for s in rest.split("/") if s]
        self.requests.append((method, "/".join(segments), None))
        if len(segments) % 2 == 0:
            fields = self.docs.get("/".join(segments))
            if fields is None:
                raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO())
            return Response({"name": self.name_prefix + "/".join(segments), "fields": fields})
        parent, collection = "/".join(segments[:-1]), segments[-1]
        rows = self.children(parent, collection)
        query = urllib.parse.parse_qs(parts.query)
        start = int((query.get("pageToken") or ["0"])[0])
        size = int((query.get("pageSize") or ["300"])[0])
        page = rows[start:start + size]
        reply = {"documents": [{"name": "%s%s/%s" % (self.name_prefix, "/".join(segments), doc_id),
                                "fields": fields} for doc_id, fields in page]}
        if start + size < len(rows):
            reply["nextPageToken"] = str(start + size)
        return Response(reply)

    def _matches(self, fields, where):
        if where is None:
            return True
        rule = where["fieldFilter"]
        value = _decode(fields.get(rule["field"]["fieldPath"], {"nullValue": None}))
        expected = _decode(rule["value"])
        if rule["op"] == "ARRAY_CONTAINS":
            return isinstance(value, list) and expected in value
        if rule["op"] == "IN":
            return value in expected
        return value == expected

    def _runQuery(self, parent, body):
        query = body["structuredQuery"]
        rows = [(doc_id, fields) for doc_id, fields in
                self.children(parent, query["from"][0]["collectionId"])
                if self._matches(fields, query.get("where"))]
        prefix = self.name_prefix + (parent + "/" if parent else "") + query["from"][0]["collectionId"]
        return [{"document": {"name": "%s/%s" % (prefix, doc_id), "fields": fields}}
                for doc_id, fields in rows] or [{"readTime": "now"}]

    def _runAggregationQuery(self, parent, body):
        query = body["structuredAggregationQuery"]["structuredQuery"]
        rows = [1 for _, fields in self.children(parent, query["from"][0]["collectionId"])
                if self._matches(fields, query.get("where"))]
        return [{"result": {"aggregateFields": {"total": {"integerValue": str(len(rows))}}}}]

    def _listCollectionIds(self, parent, body):
        return {"collectionIds": sorted(self.tree(parent))}

    def _beginTransaction(self, parent, body):
        return {"transaction": "txn"}

    def _rollback(self, parent, body):
        return {}

    def _commit(self, parent, body):
        staged = dict(self.docs)
        for write in body["writes"]:
            if "delete" in write:
                staged.pop(write["delete"][len(self.name_prefix):], None)
                continue
            path = write["update"]["name"][len(self.name_prefix):]
            precondition = write.get("currentDocument", {})
            if "exists" in precondition and (path in staged) != precondition["exists"]:
                raise urllib.error.HTTPError("commit", 409, "precondition failed", {}, io.BytesIO())
            if "updateMask" in write:
                merged = dict(staged.get(path, {}))
                for field in write["updateMask"]["fieldPaths"]:
                    merged[field] = write["update"]["fields"][field]
                staged[path] = merged
            else:
                staged[path] = write["update"]["fields"]
        self.docs = staged
        return {"writeResults": [{} for _ in body["writes"]]}
