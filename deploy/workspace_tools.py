"""What the workspace tools share: naming a document tree, and the few
Firestore REST calls a copy or a seed needs.

``root`` is the root collections a single-operator install keeps its data in;
``workspace:<id>`` is ``workspaces/<id>/``. Paths always come from
``operator_config.documents_root`` -- the function the bot itself uses -- so a
tool can never write somewhere the bot would not read.
"""

import json
import os
import time
import urllib.parse
import urllib.request

import operator_config
import operator_writer

COMMIT_BATCH = operator_writer.COMMIT_BATCH


class ToolError(Exception):
    pass


def parse_side(value):
    """``root`` -> ``None``; ``workspace:<id>`` -> ``<id>``."""
    if value == "root":
        return None
    if value.startswith("workspace:"):
        wid = value[len("workspace:"):]
        if operator_config.WORKSPACE_ID_RE.match(wid):
            return wid
    raise ToolError("%r is not 'root' or 'workspace:<id>'" % value)


def side_env(env, wid):
    """An environment whose document root is this side, so the paths come from
    exactly the function the bot itself uses."""
    out = dict(env)
    out.pop(operator_config.WORKSPACE_ENV, None)
    if wid is not None:
        out[operator_config.WORKSPACE_ENV] = wid
    return out


def label(wid):
    return "root" if wid is None else "workspaces/%s" % wid


class Firestore:
    """The four calls a copy needs, over the REST API the bot already uses."""

    def __init__(self, creds, token, *, opener, sleep):
        self.project = creds["project_id"]
        self.token = token
        self.opener = opener
        self.sleep = sleep

    def _post(self, url, body):
        return operator_config._request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers=dict(operator_config._auth(self.token), **{"Content-Type": "application/json"}),
            opener=self.opener, sleep=self.sleep)

    def collection_ids(self, env):
        parent = operator_config.documents_root(urllib.parse.quote(self.project, safe=""), env)
        out, page = [], None
        while True:
            body = {"pageSize": 300}
            if page:
                body["pageToken"] = page
            reply = self._post("%s/%s:listCollectionIds" % (operator_config.API_ROOT, parent), body)
            out.extend(reply.get("collectionIds") or [])
            page = reply.get("nextPageToken")
            if not page:
                return sorted(out)

    def documents(self, env, collection):
        return dict(operator_config.list_documents(self.project, collection, self.token,
                                                   opener=self.opener, sleep=self.sleep, env=env))

    def name(self, env, collection, doc_id):
        """A document's resource name, as writes address it."""
        return "%s/%s/%s" % (operator_config.documents_root(self.project, env), collection, doc_id)

    def commit(self, operations):
        url = "%s/%s:commit" % (operator_config.API_ROOT,
                                operator_config.database_documents(self.project))
        for start in range(0, len(operations), COMMIT_BATCH):
            self._post(url, {"writes": operations[start:start + COMMIT_BATCH]})


def connect(key, env, *, opener=None, signer=None, now=None, sleep=None, cache_dir):
    env = dict(env)
    if key:
        env["GCP_SERVICE_ACCOUNT_FILE"] = key
    # Name the key or inherit one; never pick up the base64 runtime variable.
    env.pop("GCP_SERVICE_ACCOUNT_B64", None)
    creds = operator_config.credentials(env)
    if creds is None:
        raise ToolError("no key: pass --key or set GCP_SERVICE_ACCOUNT_FILE")
    opener = opener or urllib.request.urlopen
    sleep = sleep or time.sleep
    token = operator_config.access_token(
        creds, os.path.join(cache_dir, "token.json"), opener=opener,
        signer=signer or operator_config.node_signer, now=now or time.time, sleep=sleep)
    return Firestore(creds, token, opener=opener, sleep=sleep), env


