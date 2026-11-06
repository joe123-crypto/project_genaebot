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

Environment:
  CONSOLE_API_TOKEN   required, >= MIN_TOKEN_LENGTH chars; the server refuses to
                      start without it, so it can never come up unauthenticated
  CONSOLE_API_PORT    default 18790
  CONSOLE_API_BIND    default 0.0.0.0
  CONSOLE_READ_BIN    default $OPENCLAW_STATE_DIR/workspace/bin/console-read
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
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# Also works when loaded by importlib in the deployment tests.
sys.path.insert(0, os.path.dirname(__file__))
from console_accounts import AccountControls

PREFIX = "/internal/console/"
DATA_ROUTES = ("overview", "groups", "campaigns")
SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")
DATE_RE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")

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
    state = os.environ.get("OPENCLAW_STATE_DIR") or DEFAULT_STATE_DIR
    return os.path.join(state, "workspace", "bin", "console-read")


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

    def store(self, key, body):
        now = time.monotonic()
        with self.lock:
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
        body = self.cached(key)
        if body is not None:
            return 200, body

        if not self.slots.acquire(blocking=False):
            log("busy: refused %s" % " ".join(args))
            return 503, b'{"error": "busy"}'
        try:
            body = self.cached(key)
            if body is not None:
                return 200, body
            with self.lock:
                self.runs += 1
            try:
                done = subprocess.run(
                    [sys.executable, "-I", self.path] + list(args),
                    capture_output=True, timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                log("timeout after %ss: %s" % (self.timeout, " ".join(args)))
                return 504, b'{"error": "bridge timed out"}'
            except OSError as exc:
                log("cannot run %s: %s" % (self.path, exc))
                return 502, b'{"error": "bridge unavailable"}'

            if done.returncode == 0:
                self.store(key, done.stdout)
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


def curated(stdout):
    """True when stdout is the bridge's own single-key error document."""
    try:
        payload = json.loads(stdout or b"")
    except ValueError:
        return False
    return isinstance(payload, dict) and set(payload) == {"error"} \
        and isinstance(payload["error"], str)


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
            with bridge.lock:
                bridge.cache.clear()
        self.send_any(status, body, with_body)

    def do_POST(self):
        split = urlsplit(self.path)
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
    elif parts[0] == "preview" and len(parts) == 2:
        if not SLUG_RE.match(parts[1]):
            raise Refuse(404, "not found")
        args = ["preview", parts[1]]
    else:
        raise Refuse(404, "not found")

    fields = parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(fields) - {"date"})
    if unknown:
        raise Refuse(400, "unsupported query parameter: %s" % ", ".join(unknown))
    dates = fields.get("date") or []
    if len(dates) > 1:
        raise Refuse(400, "date may only be given once")
    if dates:
        if not DATE_RE.match(dates[0]):
            raise Refuse(400, "date wants YYYY-MM-DD")
        args += ["--date", dates[0]]
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


def build_server(bind, port, token, bridge_path, **kwargs):
    if len(token or "") < MIN_TOKEN_LENGTH:
        raise ValueError(
            "CONSOLE_API_TOKEN must be at least %d characters" % MIN_TOKEN_LENGTH)
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    close_server = server.server_close
    def close():
        server.console["accounts"].close()
        close_server()
    server.server_close = close
    server.console = {"token": token, "bridge": Bridge(bridge_path, **kwargs), "accounts": AccountControls()}
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
        server = build_server(bind, port, token, bridge_path)
    except ValueError as exc:
        log("%s; refusing to start" % exc)
        return 78
    except OSError as exc:
        log("cannot bind %s:%d (%s); refusing to start" % (bind, port, exc))
        return 78

    if not os.path.exists(bridge_path):
        log("warning: bridge not found at %s; requests will answer 502" % bridge_path)
    log("listening on %s:%d, bridge %s" % (bind, port, bridge_path))
    try:
        server.serve_forever()
    finally:
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
