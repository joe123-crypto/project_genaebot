"""The customer bot's published copy, kept outside the synced workspace."""

import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

TEXT_LIMIT = 16 * 1024
BODY_LIMIT = 256 * 1024
FIELDS = ("instructions",)
SOURCES = [
    {"id": "campaigns", "label": "Live campaigns", "description": "Current advertised offers and advertising schedules from the campaign store."},
    {"id": "borehole", "label": "Borehole offers", "description": "Location-based prices and packages from the existing provider catalogue."},
    {"id": "availability", "label": "Service availability", "description": "Runtime service restrictions take precedence over advertised offers."},
]
REVISION = re.compile(r"(?:default-[a-f0-9]{64}|[a-f0-9]{32})")


class Conflict(Exception):
    def __init__(self, current):
        self.current = current


def clean_text(value, label):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{label} must contain text.")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError(f"{label} contains invalid characters.") from None
    if size > TEXT_LIMIT:
        raise ValueError(f"{label} must be no larger than 16 KB.")
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def copy_fields(data):
    return {field: clean_text(data.get(field), label) for field, label in
            zip(FIELDS, ("Reply instructions",))}


class CustomerBot:
    def __init__(self, state_dir):
        self.state = Path(state_dir)
        self.docs = self.state / "workspace" / "docs"
        self.directory = self.state / "runtime" / "customer-bot"
        self.path = self.directory / "published.json"

    def defaults(self):
        values = copy_fields({
            "instructions": (self.docs / "public-whatsapp-reply-instructions.txt").read_text(encoding="utf-8"),
        })
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        return {**values, "revision": f"default-{fingerprint}", "publishedAt": None, "publishedBy": None, "sources": SOURCES}

    def read(self):
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(BODY_LIMIT + 1)
        except FileNotFoundError:
            return self.defaults()
        if len(raw) > BODY_LIMIT:
            raise ValueError("Invalid published copy size.")
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get("version") not in (1, 2)
                or not isinstance(data.get("revision"), str) or not re.fullmatch(r"[a-f0-9]{32}", data["revision"])
                or not isinstance(data.get("publishedAt"), str)
                or not isinstance(data.get("publishedBy"), str) or not data["publishedBy"]):
            raise ValueError("Invalid published copy.")
        if datetime.fromisoformat(data["publishedAt"]).utcoffset() is None:
            raise ValueError("Invalid publish time.")
        return {**copy_fields(data), **{key: data[key] for key in ("revision", "publishedAt", "publishedBy")}, "sources": SOURCES}

    def publish(self, request):
        if isinstance(request, dict) and "knowledge" in request:
            raise ValueError("Upgrade the console: business knowledge is now read from live tools. Publish reply instructions only.")
        if not isinstance(request, dict) or set(request) != {*FIELDS, "expectedRevision", "actor"}:
            raise ValueError("Invalid publish request.")
        expected = request["expectedRevision"]
        if not isinstance(expected, str) or not REVISION.fullmatch(expected):
            raise ValueError("The live revision is missing or invalid.")
        values = copy_fields(request)
        actor = clean_text(request["actor"], "Publisher name")
        if len(actor) > 120 or not actor.isprintable():
            raise ValueError("Invalid publisher name.")

        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Separate lock inode: replacing published.json must not release the lock
        # for another writer. Readers see either complete revision without a lock.
        lock_fd = os.open(self.directory / "publish.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                current = self.read()
            except (OSError, ValueError, AttributeError) as error:
                raise OSError("Current customer bot copy is unavailable") from error
            if all(values[key] == current[key] for key in FIELDS):
                return current  # also makes a replay of a completed publish a no-op
            if current["revision"] != expected:
                raise Conflict(current)
            published = {
                **values, "revision": uuid4().hex,
                "publishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "publishedBy": actor,
            }
            # Preserve each replaced publication, including the retired briefing.
            # A stable revision filename makes retrying archival idempotent.
            if self.path.exists():
                archive = self.directory / "history"
                archive.mkdir(mode=0o700, exist_ok=True)
                archived = archive / f"{current['revision']}.json"
                if not archived.exists():
                    os.link(self.path, archived)
                    archive_fd = os.open(archive, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(archive_fd)
                    finally:
                        os.close(archive_fd)
            fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=".publish-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"version": 2, **published}, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                dir_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return {**published, "sources": SOURCES}
