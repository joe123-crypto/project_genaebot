#!/usr/bin/env python3
"""Enable existing safe idle retirement for the pinned model-catalog pool.

Catalog requests supply a complete workerInput each time; the SDK's pool owns
native-section draining, worker exit and source-capture cleanup. Keep that owner
and its normal five-minute anti-churn warm window intact. This changes only the
catalog's opt-out from retirement, not database worker custody or active tasks.
"""

import argparse
import json
from pathlib import Path

VERSION = "2026.9.9"
MARKER = "// Genaie: idle model-catalog worker retirement (OpenClaw 2026.9.9)"
ANCHOR = "\t\tmaxWorkers: GATEWAY_CATALOG_WORKERS,\n\t\tidleTimeoutMs: 0,\n\t\trestartOnError: false,"
REPLACEMENT = ANCHOR.replace("idleTimeoutMs: 0", 'idleTimeoutMs: process.env.GENAIE_CATALOG_IDLE_RETIREMENT === "0" ? 0 : 60_000')


def transform(source):
    if MARKER in source:
        if source.count(REPLACEMENT) != 1 or ANCHOR in source:
            raise ValueError("catalog retirement patch marker does not match its policy")
        return source
    for evidence in ["function createCatalogPool(", "releaseResources: capture.release",
                     "value: workerInput", "requestPool.run("]:
        if evidence not in source:
            raise ValueError("model-catalog worker reconstruction/cleanup contract changed")
    if source.count(ANCHOR) != 1:
        raise ValueError("model-catalog pool patch anchor changed")
    return MARKER + "\n" + source.replace(ANCHOR, REPLACEMENT)


def patch(root, check=False):
    if json.loads((root / "package.json").read_text())["version"] != VERSION:
        raise ValueError("review catalog worker retirement before upgrading OpenClaw")
    candidates = [p for p in (root / "dist").glob("prepared-model-catalog-worker-*.mjs")
                  if "function createCatalogPool(" in p.read_text()]
    if len(candidates) != 1:
        raise ValueError("expected exactly one model-catalog pool implementation")
    path = candidates[0]
    source = path.read_text()
    patched = transform(source)
    if check and patched != source:
        raise ValueError("model-catalog retirement patch is missing")
    if not check and patched != source:
        path.write_text(patched)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print("catalog worker retirement verified:", patch(args.root, args.check).name)
