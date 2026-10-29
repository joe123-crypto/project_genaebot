#!/usr/bin/env python3
"""Shorten the existing history owner's native-safe idle rotation, opt-in only.

Do not enable automatic pool retirement: the history owner must release database
custody only after its existing rotate/join path proves worker exit. Writer,
archive/reclamation, usage-refresh and lease-heartbeat lifetimes stay upstream.
"""

import argparse
import json
from pathlib import Path

VERSION = "2026.9.9"
MARKER = "// Genaie: five-minute history-owner idle rotation (OpenClaw 2026.9.9)"
ANCHOR = "\t}, SQLITE_IDLE_HANDLE_TTL_MS));\n\tlane.idleTimer.unref();"
REPLACEMENT = ANCHOR.replace(
    "SQLITE_IDLE_HANDLE_TTL_MS",
    'process.env.GENAIE_HISTORY_IDLE_RETIREMENT === "1" && historyWorkerLanes.includes(lane) ? 300_000 : SQLITE_IDLE_HANDLE_TTL_MS',
)


def transform(source):
    if MARKER in source:
        if source.count(REPLACEMENT) != 1 or ANCHOR in source:
            raise ValueError("history retirement marker does not match its policy")
        return source
    for evidence in [
        "function armDatabaseWorkerIdleRetirement(lane)",
        "lane.nativeSequence <= lane.retiredSequence || lane.pending > 0",
        "rotateDatabaseWorkers(lane).catch",
        "lane.pool.rotate().then(() => releaseRetiredDatabaseCustody(lane, through))",
        "const historyWorkerLanes = [",
    ]:
        if evidence not in source:
            raise ValueError("history worker custody/rotation contract changed")
    if source.count(ANCHOR) != 1:
        raise ValueError("history owner idle timer anchor changed")
    return MARKER + "\n" + source.replace(ANCHOR, REPLACEMENT)


def patch(root, check=False):
    if json.loads((root / "package.json").read_text())["version"] != VERSION:
        raise ValueError("review history retirement before upgrading OpenClaw")
    paths = [p for p in (root / "dist").glob("session-transcript-worker-resources-*.mjs")
             if "function armDatabaseWorkerIdleRetirement(lane)" in p.read_text()]
    if len(paths) != 1:
        raise ValueError("expected exactly one history owner implementation")
    path = paths[0]
    source = path.read_text()
    patched = transform(source)
    if check and patched != source:
        raise ValueError("history idle patch is missing")
    if not check and patched != source:
        path.write_text(patched)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print("history owner idle rotation verified:", patch(args.root, args.check).name)
