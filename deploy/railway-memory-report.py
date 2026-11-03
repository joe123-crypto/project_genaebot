#!/usr/bin/env python3
"""Read container/process memory and existing OpenClaw worker samples over SSH.

By default: no inspector, heap snapshot, GC, restart, config change or scheduled collector.
Redirect reports outside the repository. Recorded worker samples may be stale.
--live-workers opts into a bounded temporary loopback inspector counter probe.
--release-source-cache additionally advises Linux to release clean installed-code
file pages. It never deletes files or touches runtime databases or credentials.
"""

import argparse
import base64
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

PROJECT = "a251f752-3b49-4f72-8a71-4629bf2fced0"
SERVICE = "503baf60-c897-4a77-812a-719195705031"
ENVIRONMENT = "2cae37a4-8e24-41db-87ed-9a3a7c9eff22"
PREFIX = "GENAIE_MEMORY_REPORT "
SETTINGS = ("SOURCE_COMMIT", "GENAIE_CAMPAIGN_TRANSPORT", "GENAIE_CATALOG_IDLE_RETIREMENT",
            "GENAIE_HISTORY_IDLE_RETIREMENT", "MALLOC_ARENA_MAX",
            "GENAIE_WHATSAPP_CACHE_CLEANUP", "GENAIE_OVERNIGHT_STANDBY")
MEMORY_FIELDS = ("rssBytes", "heapTotalBytes", "heapUsedBytes", "externalBytes", "arrayBuffersBytes",
                 "workerCount", "workerHeapSampledCount", "workerArrayBuffersSampledCount")


def number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def worker_sample(event, captured_ms):
    """Allow only memory counters; do not retain unrelated diagnostic data."""
    if event.get("type") != "diagnostic.memory.sample" or not number(event.get("ts")):
        raise ValueError("invalid memory sample")
    memory = event.get("memory", {})
    age = captured_ms - event["ts"]
    groups = {}
    for worker in memory.get("workerHeaps", []):
        script = worker.get("script", "other")
        # Upstream normally sanitizes names, but reports must never export paths.
        if not isinstance(script, str) or "/" in script or "\\" in script or len(script) > 120:
            script = "other"
        if not all(number(worker.get(key)) for key in ("heapUsed", "heapTotal", "external", "sampleAgeMs")):
            continue
        group = groups.setdefault(script, {"script": script, "sampledWorkers": 0,
                                           "heapUsedBytes": 0, "heapTotalBytes": 0,
                                           "externalBytes": 0, "arrayBuffersBytes": 0,
                                           "arrayBuffersSampledWorkers": 0, "maxSampleAgeMs": 0})
        group["sampledWorkers"] += 1
        for source, target in [("heapUsed", "heapUsedBytes"), ("heapTotal", "heapTotalBytes"),
                               ("external", "externalBytes")]:
            group[target] += worker[source]
        group["maxSampleAgeMs"] = max(group["maxSampleAgeMs"], worker["sampleAgeMs"])
        if number(worker.get("arrayBuffers")):
            group["arrayBuffersBytes"] += worker["arrayBuffers"]
            group["arrayBuffersSampledWorkers"] += 1
    return {"recordedAt": datetime.fromtimestamp(event["ts"] / 1000, timezone.utc).isoformat(),
            "recordAgeMs": age,
            "fresh": 0 <= age and age + max((g["maxSampleAgeMs"] for g in groups.values()), default=0) <= 60_000,
            "memory": {key: memory[key] for key in MEMORY_FIELDS if number(memory.get(key))},
            "workerMemoryScope": "direct; nested workers excluded",
            "workerGroups": sorted(groups.values(), key=lambda item: item["heapUsedBytes"], reverse=True),
            "note": "Worker counters precede the event by maxSampleAgeMs. ArrayBuffers are included in "
                    "external memory; do not add them again. Heap totals are not process RSS. "
                    "Do not subtract old samples from current process memory to infer native memory."}


def read_counters(path):
    return {parts[0].rstrip(":"): int(parts[1]) for line in path.read_text().splitlines()
            if len(parts := line.split()) >= 2 and parts[1].isdigit()}


def process_snapshot(directory):
    status = read_counters(directory / "status")
    result = {"pid": int(directory.name), "name": (directory / "comm").read_text().strip(),
              "startTicks": int((directory / "stat").read_text().rsplit(")", 1)[1].split()[19]),
              "rssBytes": status.get("VmRSS", 0) * 1024,
              "anonymousRssBytes": status.get("RssAnon", 0) * 1024,
              "fileRssBytes": status.get("RssFile", 0) * 1024,
              "threads": status.get("Threads", 0)}
    try:
        smaps = read_counters(directory / "smaps_rollup")
        result.update({"pssBytes": smaps["Pss"] * 1024,
                       "privateDirtyBytes": smaps["Private_Dirty"] * 1024})
    except (OSError, KeyError):
        result["pssUnavailable"] = True
    return result


def release_source_cache(root=Path("/usr/local/lib/node_modules/openclaw/dist")):
    """One bounded POSIX advisory over immutable code; no file contents are read."""
    started = time.monotonic()
    result = {"filesAdvised": 0, "fileBytesAdvised": 0, "errors": 0, "complete": True}
    if not hasattr(os, "posix_fadvise") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("source-cache advisory is unsupported on this platform")
    if not root.is_dir() or root.resolve() != root:
        raise ValueError("installed source directory is missing or symlinked")
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        # Do not follow even a directory symlink within the installed tree.
        subdirectories[:] = [name for name in subdirectories if not (Path(directory) / name).is_symlink()]
        for name in files:
            if time.monotonic() - started > 20:
                result["complete"] = False
                return result
            path = Path(directory) / name
            if path.suffix not in {".js", ".mjs", ".cjs", ".json"}:
                continue
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    # Installed code should be regular files, never devices/FIFOs.
                    import stat
                    status = os.fstat(descriptor)
                    if not stat.S_ISREG(status.st_mode):
                        continue
                    os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
                    result["filesAdvised"] += 1
                    result["fileBytesAdvised"] += status.st_size
                finally:
                    os.close(descriptor)
            except OSError:
                result["errors"] += 1
    return result


def container_snapshot(cgroup):
    return {"currentBytes": int((cgroup / "memory.current").read_text()),
            "peakBytes": int((cgroup / "memory.peak").read_text()),
            "memoryStat": read_counters(cgroup / "memory.stat"),
            "events": read_counters(cgroup / "memory.events")}


def remote_snapshot(release_cache=False, live_source=None):
    captured = datetime.now(timezone.utc)
    processes = []
    for directory in Path("/proc").iterdir():
        if directory.name.isdigit():
            try:
                processes.append(process_snapshot(directory))
            except (OSError, ValueError, IndexError):
                pass  # Processes may exit between proc reads.
    gateways = [item for item in processes if item["name"].startswith("openclaw-gatewa")]
    cgroup = Path("/sys/fs/cgroup")
    report = {"capturedAt": captured.isoformat(), "observerPid": os.getpid(),
              "container": container_snapshot(cgroup),
              "processes": sorted(processes, key=lambda item: item["rssBytes"], reverse=True),
              "gatewayRunning": len(gateways) == 1,
              "note": "OS counters precede the transient diagnostic CLI. The SSH/Python observer is "
                      "included in container memory. File cache, process RSS and PSS overlap; do not sum them. "
                      "These are runtime counters, not an assertion of Railway billable memory."}
    if len(gateways) != 1:
        report["diagnosticsUnavailable"] = "gateway absent or ambiguous; no process was started"
        return report
    gateway = gateways[0]
    report["gatewayPid"] = gateway["pid"]
    if release_cache:
        report["sourceCacheTrial"] = release_source_cache()
        time.sleep(2)
        report["sourceCacheTrial"]["after"] = container_snapshot(cgroup)
        report["sourceCacheTrial"]["note"] = "An advisory is not a guarantee of reclamation. " \
            "Before/after deltas include concurrent activity and do not establish sustained billing savings."
    environment = dict(item.split("=", 1) for item in
                       (Path("/proc") / str(gateway["pid"]) / "environ").read_text().split("\0") if "=" in item)
    report["runtimeSettings"] = {key: environment.get(key) for key in SETTINGS}
    # Run with the gateway's identity/config, but never return its environment or stderr.
    environment.pop("OPENCLAW_HOME", None)
    environment["OPENCLAW_GATEWAY_TOKEN"] = environment.get("GATEWAY_AUTH_TOKEN", "")
    command = ["openclaw", "gateway", "call", "diagnostics.stability", "--json", "--params",
               json.dumps({"type": "diagnostic.memory.sample", "limit": 12}), "--timeout", "10000"]
    try:
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=True, timeout=20)
        data = json.loads(result.stdout)
        received = datetime.now(timezone.utc)
        report["diagnosticsReceivedAt"] = received.isoformat()
        report["workerSamples"] = [worker_sample(event, received.timestamp() * 1000)
                                   for event in data.get("events", [])]
        # A restart would make OS and runtime measurements belong to different owners.
        current = process_snapshot(Path("/proc") / str(gateway["pid"]))
        if current["startTicks"] != gateway["startTicks"]:
            raise ValueError("gateway changed during capture")
    except (OSError, ValueError, subprocess.SubprocessError):
        report.pop("workerSamples", None)
        report["diagnosticsUnavailable"] = "diagnostic query failed or gateway changed during capture"
    if live_source:
        try:
            result = subprocess.run(["node", "--input-type=module", "-e", live_source, "--", str(gateway["pid"])],
                                    env=environment, capture_output=True, text=True, check=True, timeout=35)
            report["liveWorkers"] = json.loads(result.stdout)
            current = process_snapshot(Path("/proc") / str(gateway["pid"]))
            if current["startTicks"] != gateway["startTicks"]:
                raise ValueError("gateway changed during capture")
        except (OSError, ValueError, subprocess.SubprocessError):
            report.pop("liveWorkers", None)
            report["liveWorkersUnavailable"] = "Counter probe failed; verify loopback port 9229 is closed. " \
                "An existing inspector is never reused or closed. No raw diagnostic errors were exported."
    return report


def capture(release_cache=False, live_workers=False):
    source = Path(__file__).read_text()
    live_source = base64.b64encode(Path(__file__).with_name("railway-worker-memory.mjs").read_bytes()).decode() if live_workers else "-"
    result = subprocess.run(["railway", "ssh", "--project", PROJECT, "--service", SERVICE,
                             "--environment", ENVIRONMENT, "--", "runuser", "-u", "joseph", "--",
                             "python3", "-c", source, "--remote", "1" if release_cache else "0", live_source],
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError("Railway memory capture failed; no config changes or restarts were requested")
    lines = [line[len(PREFIX):] for line in result.stdout.splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1:
        raise ValueError("Railway did not return exactly one memory report")
    return json.loads(lines[0])


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--remote":
        print(PREFIX + json.dumps(remote_snapshot(sys.argv[2] == "1",
              base64.b64decode(sys.argv[3]).decode() if sys.argv[3] != "-" else None)))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-source-cache", action="store_true", help="one-shot Linux advisory over installed code only")
    parser.add_argument("--live-workers", action="store_true", help="fresh direct-worker counters via a temporary loopback inspector; closes it afterward")
    args = parser.parse_args()
    print(json.dumps(capture(args.release_source_cache, args.live_workers), indent=2))


if __name__ == "__main__":
    main()
