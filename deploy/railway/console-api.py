#!/usr/bin/env python3
"""Serve the operator console's read model over HTTP, beside the gateway.

Campaign and membership reads use workspace/bin/console-read, which never
writes or contacts the gateway. Account routes use a separate authenticated
gateway control bridge for live status, pairing, pause/start, and unlink.

It runs as a backgrounded child of the entrypoint, is never waited on, and wraps
main() so a fault can only ever kill this process. It does not own the
container's healthcheck, so a failure here cannot make the platform restart the
bot. The gateway keeps 18789 to itself; this listens on CONSOLE_API_PORT.

Two limits exist because this shares a container with the gateway, where the
real hazard is not a crash but unbounded subprocess fan-out exhausting memory:

  * at most MAX_CONCURRENCY bridge processes at any moment (503 beyond that)
  * successful responses are cached for CACHE_TTL_SECONDS

Read routes, GET or HEAD:

  /internal/console/health            no auth; {"ok": true} and nothing else
  /internal/console/overview          bearer
  /internal/console/groups            bearer
  /internal/console/campaigns         bearer
  /internal/console/preview/<slug>    bearer

These read routes take an optional ?date=YYYY-MM-DD.
Account routes (bearer, no cache):
  GET /internal/console/accounts
  GET /internal/console/accounts/link-sessions/<id>
  POST /internal/console/accounts                  {"id": "sales"}
  POST /internal/console/accounts/<id>/<action>    {} (link/unlink/pause/start)
The account helper holds one persistent Node bridge; conflicting requests get
409. QR sessions are temporary, while account enablement and login live in the
gateway's durable state. Request bodies are bounded to 2048 bytes.

Operator-config writes (bearer, no cache):
  POST /internal/console/config/<collection>/<id>
       {"fields": {...}, "actor": "...", "dryRun": false}      change fields
       {"delete": true, "reason": "...", "actor": "..."}      delete (groups only)

<collection> is groups, lines, campaigns or settings, and <id> is
percent-encoded because a group JID carries an "@". Every change goes through
workspace/bin/console-write, which judges it with the bot's own
promo_membership.validate and refuses anything that would stop a group being
advertised to; this process only checks that a request is worth forwarding.
Writes are serialised, never cached, and clear the read cache afterwards. They
need GCP_SERVICE_ACCOUNT_WRITE_B64 -- a key with Cloud Datastore User, separate
from the read-only runtime key the cron runs carry -- and answer 503 without it,
which is how the write surface ships inert.

Group actions (bearer, no cache):
  POST /internal/console/groups/<jid>/actions
       {"leave": true, "clearRecords": true, "account": "ads", "actor": "..."}

The two things a config delete cannot do for itself: take the account's number
out of the WhatsApp group, and forget the per-campaign send history for it. At
least one must be asked for. Neither writes Firestore, so neither needs the
write key; the leave is queued for the channel monitor, which holds the only
socket that can perform it, and a result that does not arrive in time is
reported as unconfirmed rather than failed. GENAIE_GROUP_ACTIONS=0 answers 503.

Environment:
  CONSOLE_API_TOKEN   required, >= MIN_TOKEN_LENGTH chars; the server refuses to
                      start without it, so it can never come up unauthenticated
  CONSOLE_API_PORT    default 18790
  CONSOLE_API_BIND    default 0.0.0.0
  CONSOLE_READ_BIN    default $OPENCLAW_STATE_DIR/workspace/bin/console-read
  CONSOLE_WRITE_BIN   default $OPENCLAW_STATE_DIR/workspace/bin/console-write
  CONSOLE_GROUP_ACTION_BIN
                      default $OPENCLAW_STATE_DIR/workspace/bin/console-group-action
  GENAIE_GROUP_ACTIONS
                      set to 0 to switch group actions off; the route then
                      answers 503 and the monitor's helper stays inactive
  GENAIE_PROMO_CRONS  passed through to the bridge, which needs the declared
                      cron manifest that deploy/ never stages into the image

SIGTERM is not forwarded here (tini runs without -g), so this process is killed
outright at container teardown. The gateway owns persisted state; pairing
sessions are temporary and can be resumed through a new Link attempt.

Std-lib only.
"""

import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

# Also works when loaded by importlib in the deployment tests.
sys.path.insert(0, os.path.dirname(__file__))
from console_accounts import AccountControls

PREFIX = "/internal/console/"
DATA_ROUTES = ("overview", "groups", "campaigns", "capabilities", "settings", "navigation")
SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")
DATE_RE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")

# Operator-config writes. The collections are operator_writer.EDITABLE's keys;
# this process does not import the bot's modules (it lives in the image, they
# live on the volume), so it checks the shape of a request and leaves every
# judgement about the DATA to console-write, which uses the bot's own validator.
CONFIG_COLLECTIONS = ("groups", "lines", "campaigns", "settings")
# A document id is a group JID, a line id, a campaign slug or "defaults".
DOC_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\Z")
# Statuses console-write is allowed to choose. Anything else means it returned
# something we do not understand, which is a bad gateway, not the caller's fault.
WRITE_STATUSES = frozenset((200, 400, 404, 409, 503))
WRITE_TIMEOUT_SECONDS = 30
WRITE_KEY_VAR = "GCP_SERVICE_ACCOUNT_WRITE_B64"

# Group actions: leaving a WhatsApp group and forgetting a group's local
# records. Neither touches Firestore, so neither is gated on the write key --
# the capability here is the gateway, not a credential.
GROUP_ACTION_KEYS = frozenset(("leave", "clearRecords", "account", "actor"))
GROUP_ACTIONS_VAR = "GENAIE_GROUP_ACTIONS"

MIN_TOKEN_LENGTH = 32
MAX_CONCURRENCY = 2
CACHE_TTL_SECONDS = 30.0
CACHE_MAX_ENTRIES = 32
BRIDGE_TIMEOUT_SECONDS = 60
BRIDGE_EXIT_NOT_FOUND = 3  # console-read's EXIT_NOT_FOUND
DEFAULT_PORT = 18790  # beside the gateway's 18789, clear of common dev ports
DEFAULT_BIND = "0.0.0.0"
DEFAULT_STATE_DIR = "/home/joseph/.openclaw"


def log(message):
    sys.stdout.write("[console-api] %s\n" % message)
    sys.stdout.flush()


def default_bridge():
    return _volume_bin("console-read")


def default_write_bridge():
    return _volume_bin("console-write")


def default_action_bridge():
    return _volume_bin("console-group-action")


def _volume_bin(name):
    state = os.environ.get("OPENCLAW_STATE_DIR") or DEFAULT_STATE_DIR
    return os.path.join(state, "workspace", "bin", name)


class Bridge:
    """Runs console-read under a concurrency cap, with a short success cache."""

    def __init__(self, path, timeout=BRIDGE_TIMEOUT_SECONDS,
                 concurrency=MAX_CONCURRENCY, ttl=CACHE_TTL_SECONDS):
        self.path = path
        self.timeout = timeout
        self.ttl = ttl
        self.slots = threading.BoundedSemaphore(concurrency)
        self.lock = threading.Lock()
        self.cache = {}
        self.runs = 0
        self.generation = secrets.token_hex(16)
        self.config_generation = self.generation

    def invalidate(self, config=True):
        with self.lock:
            self.cache.clear()
            self.generation = secrets.token_hex(16)
            if config:
                self.config_generation = self.generation

    def cached(self, key):
        now = time.monotonic()
        with self.lock:
            entry = self.cache.get(key)
            if entry is None:
                return None
            expires, body = entry
            if expires <= now:
                del self.cache[key]
                return None
            return body

    def store(self, key, body, generation=None):
        now = time.monotonic()
        with self.lock:
            if generation is not None and generation != self.generation:
                return
            # Drop whatever expired before trimming, so a burst of distinct
            # requests cannot evict live entries while dead ones sit there.
            for stale in [k for k, (expires, _) in self.cache.items() if expires <= now]:
                del self.cache[stale]
            while len(self.cache) >= CACHE_MAX_ENTRIES:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (now + self.ttl, body)

    def run(self, args):
        """Return (status, body). Never raises, and never leaks stderr."""
        key = tuple(args)
        fresh = "--fresh-config" in args
        body = None if fresh else self.cached(key)
        if body is not None:
            return 200, body

        if not self.slots.acquire(blocking=False):
            log("busy: refused %s" % " ".join(args))
            return 503, b'{"error": "busy"}'
        try:
            body = None if fresh else self.cached(key)
            if body is not None:
                return 200, body
            with self.lock:
                self.runs += 1
                generation = self.generation
                config_generation = self.config_generation
            try:
                done = subprocess.run(
                    [sys.executable, "-I", self.path] + list(args),
                    capture_output=True, timeout=self.timeout,
                    env={**os.environ, "GENAIE_CONSOLE_CONFIG_GENERATION": config_generation},
                )
            except subprocess.TimeoutExpired:
                log("timeout after %ss: %s" % (self.timeout, " ".join(args)))
                return 504, b'{"error": "bridge timed out"}'
            except OSError as exc:
                log("cannot run %s: %s" % (self.path, exc))
                return 502, b'{"error": "bridge unavailable"}'

            if done.returncode == 0:
                if not fresh:
                    self.store(key, done.stdout, generation)
                return 200, done.stdout

            # Something the caller named does not exist. That is their error,
            # not a failure of ours, so it must not read as a bad gateway.
            if done.returncode == BRIDGE_EXIT_NOT_FOUND and curated(done.stdout):
                return 404, done.stdout

            # The bridge's own {"error": ...} is a curated message and safe to
            # pass on. Anything else means it crashed, and stderr may carry a
            # traceback or a path, so that only ever reaches the container log.
            log("bridge exited %d: %s | %s" % (
                done.returncode,
                (done.stderr or b"").decode("utf-8", "replace").strip()[:500],
                " ".join(args),
            ))
            if curated(done.stdout):
                return 502, done.stdout
            return 502, b'{"error": "bridge failed"}'
        finally:
            self.slots.release()


class WriteBridge:
    """Runs console-write, one change at a time.

    Serialised rather than capped: ``operator_writer`` reads the live config,
    applies one patch and writes the whole document back, so two writes running
    at once could each miss the other's change. One at a time removes that race
    for everything coming through this API. It cannot remove it for a Firebase
    Console edit made at the same moment -- Firestore has no transaction here and
    the Console does not come through this path -- so the audit trail is what
    tells an operator what actually happened.

    Nothing is cached: a write's answer is about this moment, and serving a
    stale one would tell an operator a change landed that did not."""

    def __init__(self, path, timeout=WRITE_TIMEOUT_SECONDS, label=None):
        self.path = path
        self.timeout = timeout
        self.lock = threading.Lock()
        self.runs = 0
        # What a log line calls the thing being changed. Group actions name a
        # jid rather than a collection and id.
        self.label = label or (lambda request: "%s/%s" % (request.get("collection"),
                                                          request.get("id")))

    def run(self, request):
        """Return (status, body). Never raises, and never leaks stderr."""
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        with self.lock:
            self.runs += 1
            try:
                done = subprocess.run(
                    [sys.executable, "-I", self.path], input=payload,
                    capture_output=True, timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                # The change may or may not have landed. Say so rather than
                # implying it did not; a caller re-reads instead of retrying.
                log("write timed out after %ss: %s"
                    % (self.timeout, self.label(request)))
                return 504, b'{"error": "the change may or may not have been ' \
                             b'applied; re-read before retrying"}'
            except OSError as exc:
                log("cannot run %s: %s" % (self.path, exc))
                return 502, b'{"error": "write bridge unavailable"}'

        status = write_status(done.stdout)
        if status is None:
            log("write bridge exited %d: %s | %s" % (
                done.returncode,
                (done.stderr or b"").decode("utf-8", "replace").strip()[:500],
                self.label(request)))
            return 502, b'{"error": "write bridge failed"}'
        return status, done.stdout


def write_status(stdout):
    """The status console-write chose, or ``None`` when its answer is not one
    this process is willing to pass on."""
    try:
        payload = json.loads(stdout or b"")
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return None
    status = payload.get("status")
    if status in WRITE_STATUSES and payload["ok"] == (status == 200):
        return status
    return None


def curated(stdout):
    """True when stdout is the bridge's own single-key error document."""
    try:
        payload = json.loads(stdout or b"")
    except ValueError:
        return False
    return isinstance(payload, dict) and set(payload) <= {"error", "code", "upstreamStatus", "quota", "retryAfterSeconds"} \
        and isinstance(payload.get("error"), str)


class Handler(BaseHTTPRequestHandler):
    server_version = "genaie-console-api"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def log_message(self, fmt, *args):
        log(fmt % args)

    def do_GET(self):
        self.respond(with_body=True)

    def do_HEAD(self):
        self.respond(with_body=False)

    def send_any(self, status, body, with_body, headers=()):
        # Anything refused may have an unread body behind it, which a kept-alive
        # HTTP/1.1 connection would then read as the next request.
        if status >= 400:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def refuse_other_methods(self):
        self.send_any(405, b'{"error": "method not allowed"}', True,
                      headers=(("Allow", "GET, HEAD"),))

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = refuse_other_methods

    def account_request(self, route, query, method, with_body=True):
        """Account routes never enter the read bridge or its 30-second cache."""
        if query:
            self.send_any(400, b'{"error":"Account routes take no query parameters."}', with_body)
            return
        parts = route.split("/")
        request = None
        if method == "GET" and parts == ["accounts"]:
            request = {"action": "list"}
        elif method == "GET" and len(parts) == 3 and parts[:2] == ["accounts", "link-sessions"]:
            if re.fullmatch(r"[a-f0-9-]{36}", parts[2]):
                request = {"action": "link-status", "sessionId": parts[2]}
        elif method == "POST":
            try:
                size = int(self.headers.get("Content-Length") or "0")
                if self.headers.get("Transfer-Encoding") or not 0 < size <= 2048:
                    raise ValueError()
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    raise ValueError()
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError()
                if parts == ["accounts"] and set(payload) == {"id"}:
                    request = {"action": "link", "accountId": payload["id"], "create": True}
                elif (len(parts) == 3 and parts[0] == "accounts"
                      and parts[2] in {"link", "unlink", "pause", "start"} and not payload):
                    request = {"action": parts[2], "accountId": parts[1]}
                if request and (not isinstance(request["accountId"], str) or
                                not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", request["accountId"])):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                self.send_any(400, b'{"error":"Invalid account request."}', True)
                return
        if request is None:
            self.send_any(404, b'{"error":"not found"}', with_body)
            return
        status, body = self.server.console["accounts"].run(request)
        # An uncertain write may still have reached the gateway; clear cached
        # configuration reads even on failure, and never replay it automatically.
        if method == "POST":
            bridge = self.server.console["bridge"]
            bridge.invalidate(config=False)
        self.send_any(status, body, with_body)

    def config_request(self, route, query, with_body=True):
        """POST /internal/console/config/<collection>/<id>

        Body: {"fields": {...}, "actor": "...", "dryRun": false}. The id is
        percent-encoded by the caller, because a group JID carries an "@".

        This checks only that the request has a shape worth forwarding. What a
        legal value is, and whether the change would stop a group being
        advertised to, is console-write's judgement -- it has the bot's own
        validator and this process does not."""
        if query:
            self.send_any(400, b'{"error":"Config routes take no query parameters."}',
                          with_body)
            return
        if not (os.environ.get(WRITE_KEY_VAR) or "").strip():
            # The key is the capability. Without it the route is honest about why
            # rather than pretending it does not exist, because an operator who
            # has just set a token needs to know what else is missing.
            self.send_any(503, b'{"error":"Operator-config writes are not enabled '
                               b'on this gateway."}', with_body)
            return
        # The route is settled before the body is looked at, so "no such
        # document" and "that change is not a change" cannot answer alike.
        parts = route.split("/")
        is_batch = route == "config/batch/groups"
        try:
            doc_id = unquote(parts[2]) if len(parts) == 3 else ""
        except UnicodeError:
            doc_id = ""
        if not is_batch and not (len(parts) == 3 and parts[0] == "config"
                and parts[1] in CONFIG_COLLECTIONS and DOC_ID_RE.match(doc_id)):
            self.send_any(404, b'{"error":"not found"}', with_body)
            return

        try:
            size = int(self.headers.get("Content-Length") or "0")
            if self.headers.get("Transfer-Encoding") or not 0 < size <= (64 * 1024 if is_batch else 2048):
                raise ValueError()
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise ValueError()
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError()
            if set(payload) - ({"changes", "actor", "dryRun"} if is_batch else {"fields", "delete", "reason", "actor", "dryRun"}):
                raise ValueError()
            # Exactly one operation: change named fields, or delete the document.
            # Which collections may be deleted, and what a legal reason is, is
            # console-write's call; this only refuses a request with no clear verb.
            if is_batch and (not isinstance(payload.get("changes"), list) or not 1 <= len(payload["changes"]) <= 100):
                raise ValueError()
            if not is_batch and ("fields" in payload) == ("delete" in payload):
                raise ValueError()
            if "fields" in payload and not isinstance(payload["fields"], dict):
                raise ValueError()
            if "delete" in payload and payload["delete"] is not True:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            self.send_any(400, b'{"error":"Invalid config change."}', True)
            return

        request = {"collection": "groups"} if is_batch else {"collection": parts[1], "id": doc_id}
        for name in ("fields", "delete", "reason", "actor", "dryRun", "changes"):
            if name in payload:
                request[name] = payload[name]

        bridge = self.server.console["bridge"]
        if not request.get("dryRun"):
            bridge.invalidate()
        status, body = self.server.console["writes"].run(request)
        # A change the caller can see means every cached read of it is wrong. An
        # uncertain write may still have landed, so clear on failure too.
        bridge.invalidate(config=not request.get("dryRun"))
        if (status == 200 and request["collection"] == "campaigns" and not request.get("dryRun")
                and self.server.console.get("reconcileWake") is not None):
            self.server.console["reconcileWake"].set()
        self.send_any(status, body, with_body)

    def campaign_create_request(self, split, delete_slug=None):
        if not authorized(self.headers.get("Authorization"), self.server.console["token"]):
            self.send_any(401, b'{"error":"unauthorized"}', True)
            return
        if not (os.environ.get(WRITE_KEY_VAR) or "").strip():
            self.send_any(503, b'{"error":"Campaign writes are not enabled."}', True)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if split.query or not 0 < length <= 60 * 1024 or self.headers.get("Transfer-Encoding"):
                raise ValueError("invalid request size")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("use application/json")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete request")
            request = json.loads(raw)
            if not isinstance(request, dict) or "reconcile" in request or "delete" in request:
                raise ValueError("invalid campaign request")
        except (ValueError, TypeError):
            self.send_any(400, b'{"error":"Invalid campaign creation request."}', True)
            return
        if delete_slug is not None:
            if set(request) - {"actor"}:
                self.send_any(400, b'{"error":"Invalid campaign deletion request."}', True)
                return
            request = {"delete": delete_slug, **request}
        bridge = self.server.console["bridge"]
        bridge.invalidate()
        status, body = self.server.console["campaigns"].run(request)
        bridge.invalidate()
        if delete_slug is not None and self.server.console.get("reconcileWake") is not None:
            self.server.console["reconcileWake"].set()
        self.send_any(status, body, True)

    def schedule_request(self, split):
        if not authorized(self.headers.get("Authorization"), self.server.console["token"]):
            self.send_any(401, b'{"error":"unauthorized"}', True)
            return
        if not (os.environ.get(WRITE_KEY_VAR) or "").strip():
            self.send_any(503, b'{"error":"Schedule writes are not enabled."}', True)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if split.query or not 0 < size <= 60 * 1024 or self.headers.get("Transfer-Encoding"):
                raise ValueError()
            if self.headers.get_content_type() != "application/json":
                raise ValueError()
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError()
            request = json.loads(raw)
            if not isinstance(request, dict) or set(request) - {"changes", "date", "timezone", "actor"}:
                raise ValueError()
        except (ValueError, TypeError):
            self.send_any(400, b'{"error":"Invalid schedule change."}', True)
            return
        bridge = self.server.console["bridge"]
        bridge.invalidate()
        status, body = self.server.console["schedules"].run(request)
        bridge.invalidate()
        # Unknown outcomes can also have committed desired state.
        if self.server.console.get("reconcileWake") is not None:
            self.server.console["reconcileWake"].set()
        self.send_any(status, body, True)

    def group_action_request(self, route, query, with_body=True):
        """POST /internal/console/groups/<jid>/actions

        Body: {"leave": true, "clearRecords": true, "account": "ads",
        "actor": "..."}. The jid is percent-encoded by the caller, because it
        carries an "@".

        Unlike the config route this writes no Firestore document and needs no
        write key: it takes the account's number out of a WhatsApp group and
        forgets what the bot stored about it. Both are the gateway's to do, so
        what gates this route is whether group actions are switched on, and the
        judgement about any particular value is console-group-action's.
        """
        if query:
            self.send_any(400, b'{"error":"Group action routes take no query '
                               b'parameters."}', with_body)
            return
        if (os.environ.get(GROUP_ACTIONS_VAR) or "").strip() == "0":
            self.send_any(503, b'{"error":"Group actions are switched off on this '
                               b'gateway."}', with_body)
            return
        parts = route.split("/")
        try:
            doc_id = unquote(parts[1]) if len(parts) == 3 else ""
        except UnicodeError:
            doc_id = ""
        if not (len(parts) == 3 and parts[0] == "groups" and parts[2] == "actions"
                and DOC_ID_RE.match(doc_id)):
            self.send_any(404, b'{"error":"not found"}', with_body)
            return

        try:
            size = int(self.headers.get("Content-Length") or "0")
            if self.headers.get("Transfer-Encoding") or not 0 < size <= 2048:
                raise ValueError()
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise ValueError()
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict) or set(payload) - GROUP_ACTION_KEYS:
                raise ValueError()
            for name in ("leave", "clearRecords"):
                if name in payload and not isinstance(payload[name], bool):
                    raise ValueError()
            # A request that asks for nothing is a mistake, not a no-op: the
            # caller meant to tick something and would be told it was done.
            if not (payload.get("leave") or payload.get("clearRecords")):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            self.send_any(400, b'{"error":"Invalid group action."}', True)
            return

        request = {"jid": doc_id}
        for name in ("leave", "clearRecords", "account", "actor"):
            if name in payload:
                request[name] = payload[name]

        status, body = self.server.console["actions"].run(request)
        # Leaving a group changes what discovery will list, so every cached read
        # of the groups payload is now wrong.
        bridge = self.server.console["bridge"]
        with bridge.lock:
            bridge.cache.clear()
        self.send_any(status, body, with_body)

    def do_POST(self):
        live = self.server.console.get("live")
        if live is not None:
            live.begin_write()
        try:
            return self._do_POST()
        finally:
            live = self.server.console.get("live")
            if live is not None:
                live.fence()

    def _do_POST(self):
        split = urlsplit(self.path)
        deletion = re.fullmatch(re.escape(PREFIX) + r"campaigns/([a-z0-9][a-z0-9-]{0,63})/delete", split.path)
        if deletion:
            self.campaign_create_request(split, deletion.group(1))
            return
        if split.path == PREFIX + "schedule":
            self.schedule_request(split)
            return
        if split.path == PREFIX + "campaigns/create":
            self.campaign_create_request(split)
            return
        if split.path.startswith(PREFIX + "config/"):
            if not authorized(self.headers.get("Authorization"),
                              self.server.console["token"]):
                self.send_any(401, b'{"error":"unauthorized"}', True)
                return
            self.config_request(split.path[len(PREFIX):], split.query)
            return
        if split.path.startswith(PREFIX + "groups/"):
            if not authorized(self.headers.get("Authorization"),
                              self.server.console["token"]):
                self.send_any(401, b'{"error":"unauthorized"}', True)
                return
            self.group_action_request(split.path[len(PREFIX):], split.query)
            return
        if not split.path.startswith(PREFIX + "accounts"):
            self.refuse_other_methods()
            return
        if not authorized(self.headers.get("Authorization"), self.server.console["token"]):
            self.send_any(401, b'{"error":"unauthorized"}', True)
            return
        self.account_request(split.path[len(PREFIX):], split.query, "POST")

    def respond(self, with_body):
        config = self.server.console
        split = urlsplit(self.path)

        # Read-only means no request ever carries a body, so refuse one rather
        # than read it. A length we cannot parse is refused the same way.
        try:
            declared = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            declared = -1
        if declared != 0 or self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self.send_any(400, b'{"error": "unexpected request body"}', with_body)
            return

        if not split.path.startswith(PREFIX):
            self.send_any(404, b'{"error": "not found"}', with_body)
            return
        route = split.path[len(PREFIX):]

        if route == "health":
            self.send_any(200, b'{"ok": true}', with_body)
            return

        if not authorized(self.headers.get("Authorization"), config["token"]):
            self.send_any(401, b'{"error": "unauthorized"}', with_body,
                          headers=(("WWW-Authenticate", "Bearer"),))
            return

        if route == "accounts" or route.startswith("accounts/"):
            if not with_body:
                self.send_any(405, b'{"error":"Use GET for account status."}', False)
                return
            self.account_request(route, split.query, "GET", with_body)
            return

        if route.startswith("live/"):
            view = route[5:]
            if view not in ("groups", "campaigns", "schedule", "metrics", "discoveries", "navigation", "campaign", "detail", "preview", "group", "status"):
                self.send_any(404, b'{"error":"Unknown live view."}', with_body)
                return
            try:
                with config["liveLock"]:
                    if config.get("live") is None:
                        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(config["bridge"].path)), "lib"))
                        from console_live_api import LiveViews
                        config["live"] = LiveViews(config["bridge"].path)
                payload = config["live"].handle(view, split.query)
                self.send_any(200, json.dumps(payload).encode(), with_body)
            except ValueError:
                self.send_any(400, b'{"error":"Invalid live-view query."}', with_body)
            except Exception:
                self.send_any(503, b'{"error":"Live view is synchronizing or unavailable. Previous data is retained."}', with_body,
                              headers=(("Retry-After", "5"),))
            return

        try:
            args = plan(route, split.query)
        except Refuse as exc:
            self.send_any(exc.status, exc.body, with_body)
            return

        status, body = config["bridge"].run(args)
        headers = (("Retry-After", "1"),) if status == 503 else ()
        self.send_any(status, body, with_body, headers=headers)


class Refuse(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.body = json.dumps({"error": message}).encode("utf-8")


def plan(route, query):
    """Map a validated route and query onto console-read arguments."""
    parts = route.split("/")
    if parts[0] in DATA_ROUTES and len(parts) == 1:
        args = [parts[0]]
    elif parts[0] in ("preview", "captions", "campaign", "campaign-state", "campaign-groups") and len(parts) == 2:
        if not SLUG_RE.match(parts[1]):
            raise Refuse(404, "not found")
        args = [parts[0], parts[1]]
    elif parts[0] in ("group", "groups-by-id") and len(parts) == 2:
        jid = unquote(parts[1])
        jids = jid.split(",") if parts[0] == "groups-by-id" else [jid]
        if not 1 <= len(jids) <= 100 or any(not DOC_ID_RE.match(item) or not item.endswith("@g.us") for item in jids):
            raise Refuse(404, "not found")
        args = [parts[0], jid]
    else:
        raise Refuse(404, "not found")

    fields = parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(fields) - {"date", "fresh"})
    if unknown:
        raise Refuse(400, "unsupported query parameter: %s" % ", ".join(unknown))
    dates = fields.get("date") or []
    if len(dates) > 1:
        raise Refuse(400, "date may only be given once")
    if dates:
        if not DATE_RE.match(dates[0]):
            raise Refuse(400, "date wants YYYY-MM-DD")
        args += ["--date", dates[0]]
    if "fresh" in fields:
        if fields["fresh"] != ["1"]:
            raise Refuse(400, "fresh must be given once with value 1")
        args += ["--fresh-config"]
    return args


def authorized(header, token):
    presented = ""
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()
    # Compare even when nothing was presented, so a missing header and a wrong
    # one cost the same.
    return hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))


def build_server(bind, port, token, bridge_path, write_path=None,
                 action_path=None, **kwargs):
    if len(token or "") < MIN_TOKEN_LENGTH:
        raise ValueError(
            "CONSOLE_API_TOKEN must be at least %d characters" % MIN_TOKEN_LENGTH)
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    close_server = server.server_close
    def close():
        server.console["accounts"].close()
        if server.console.get("live") is not None:
            server.console["live"].close()
        close_server()
    server.server_close = close
    server.console = {"live": None, "liveLock": threading.Lock(), "token": token, "bridge": Bridge(bridge_path, **kwargs),
                      "accounts": AccountControls(),
                      "writes": WriteBridge(write_path or default_write_bridge()),
                      "schedules": WriteBridge(_volume_bin("console-schedule"), timeout=30),
                      "campaigns": WriteBridge(_volume_bin("console-campaign-create"), timeout=120),
                      "actions": WriteBridge(
                          action_path or default_action_bridge(),
                          label=lambda request: "groups/%s" % request.get("jid"))}
    server.console["schedules"].lock = server.console["writes"].lock
    server.console["campaigns"].lock = server.console["writes"].lock
    return server


def main():
    token = os.environ.get("CONSOLE_API_TOKEN") or ""
    bridge_path = os.environ.get("CONSOLE_READ_BIN") or default_bridge()
    bind = os.environ.get("CONSOLE_API_BIND") or DEFAULT_BIND
    try:
        port = int(os.environ.get("CONSOLE_API_PORT") or DEFAULT_PORT)
    except ValueError:
        log("CONSOLE_API_PORT is not a number; refusing to start")
        return 78

    try:
        server = build_server(
            bind, port, token, bridge_path,
            os.environ.get("CONSOLE_WRITE_BIN") or default_write_bridge(),
            os.environ.get("CONSOLE_GROUP_ACTION_BIN") or default_action_bridge())
    except ValueError as exc:
        log("%s; refusing to start" % exc)
        return 78
    except OSError as exc:
        log("cannot bind %s:%d (%s); refusing to start" % (bind, port, exc))
        return 78

    if not os.path.exists(bridge_path):
        log("warning: bridge not found at %s; requests will answer 502" % bridge_path)
    # A durable Firestore desired schedule is retried after API/gateway restart.
    stop_reconcile = threading.Event()
    wake_reconcile = threading.Event()
    server.console["reconcileWake"] = wake_reconcile
    def reconcile_campaigns():
        delay = 300
        while not stop_reconcile.is_set():
            wake_reconcile.clear()
            if (os.environ.get(WRITE_KEY_VAR) or "").strip():
                status, body = server.console["campaigns"].run({"reconcile": True})
                deletions_pending = schedule_pending = 0
                try:
                    result = json.loads(body)
                    pending = result.get("pending", 0)
                    schedule_pending = result.get("schedulePending", 0)
                    deletions_pending = result.get("deletionsPending", 0)
                    if result.get("deletionsChanged"):
                        server.console["bridge"].invalidate()
                except (ValueError, AttributeError):
                    pending = 1
                if status == 200 and (deletions_pending or schedule_pending):
                    delay = 60
                elif status != 200 or pending:
                    log("campaign schedule reconciliation is pending")
                    delay = min(delay * 2, 3600)
                else:
                    delay = 300
            wake_reconcile.wait(delay)
    threading.Thread(target=reconcile_campaigns, daemon=True).start()
    log("listening on %s:%d, bridge %s" % (bind, port, bridge_path))
    try:
        server.serve_forever()
    finally:
        stop_reconcile.set()
        wake_reconcile.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except BaseException as exc:  # never take anything else down with us
        log("fatal: %s: %s" % (type(exc).__name__, exc))
        sys.exit(1)
