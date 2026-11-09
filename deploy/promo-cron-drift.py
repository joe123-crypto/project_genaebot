#!/usr/bin/env python3
"""Compare the gateway's live promo cron state against deploy/promo-crons.json.

This script NEVER contacts a gateway: it reads a `openclaw cron list --all --json`
payload from a file or stdin, so `reinstall-promo-crons.sh --check` is read-only by
construction rather than by discipline, and every comparison case below is a plain
unit test over synthetic JSON.

Gated fields (a mismatch is drift): name, enabled, schedule.expr, schedule.tz,
payload.timeoutSeconds, payload.argv. argv is gated because promo_broadcast's
cron_timeout_seconds() finds its own job by the argv path tail, so a wrong argv both
runs the wrong script and breaks that lookup -- and register_promo_cron's own
self-verify does not cover it.

Reported but NOT gated: payload.cwd and failureAlert.*. Nothing in this repo observes
which JSON keys the gateway reports those under, and gating on a guessed path would
report false drift on all the promos, which is worse than today's silence. The first
live --check reveals the real shape; promoting them to the gate is then a one-liner.

Console-owned jobs are identified by both their name and shared-runner command,
then reported separately: their schedules belong to the Firestore reconciler.

Exit codes: 0 in sync, 1 drift/missing/legacy/undeclared, 2 bad input or manifest.
"""

import argparse
import json
import os
import re
import sys

# Candidate keys for the working directory: install-promo.sh passes --command-cwd, but
# which key `cron list --json` echoes it under is unverified. Probe, report, never gate.
CWD_KEYS = ("cwd", "workdir", "workingDirectory", "commandCwd", "command_cwd")

PROMO_ARGV_RE = re.compile(r"(?:^|/)cron/([a-z0-9-]+-promo)/")
CONSOLE_JOB_RE = re.compile(r"console-([a-z0-9][a-z0-9-]{0,63})-promo")


def console_owned(manifest, name, argv):
    """Recognize the reconciler's namespace AND its exact shared-runner command.

    A name prefix alone must not exempt a leftover manifest script from checks.
    The console reconciler owns these schedules; the static manifest does not.
    """
    match = CONSOLE_JOB_RE.fullmatch(name)
    cwd = manifest.get("commandCwd")
    return bool(match and cwd and argv == [
        "python3", os.path.join(cwd, "bin", "run-console-campaign"), match.group(1)])


def redact(number):
    """+263785632943 -> +2637...943. Keeps drift reports safe to paste into a log."""
    text = str(number)
    if len(text) <= 8:
        return text
    return "%s...%s" % (text[:5], text[-3:])


def load_manifest(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    promos = data.get("promos")
    if not isinstance(promos, list) or not promos:
        raise ValueError("manifest has no promos")
    timeout = data.get("timeoutSeconds")
    if not isinstance(timeout, int):
        raise ValueError("manifest has no integer timeoutSeconds")
    for promo in promos:
        if type(promo.get("enabled", True)) is not bool:
            raise ValueError("promo enabled must be a boolean")
        for key in ("slug", "name", "cron", "tz", "script"):
            if not promo.get(key):
                raise ValueError("promo %r is missing %s" % (promo.get("slug"), key))
    return data


def load_live(path):
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    data = json.loads(raw)  # a JSONDecodeError here is an exit-2 condition
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError('live state has no "jobs" list')
    return jobs


def as_seconds(value):
    """The gateway's JSON type for timeoutSeconds is not pinned anywhere in this repo."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def compare_one(promo, job, timeout):
    """Return the list of gated fields that differ, as (field, declared, live)."""
    declared_argv = ["python3", promo["script"]]
    checks = [
        ("enabled", promo.get("enabled", True), job.get("enabled")),
        ("schedule.expr", promo["cron"], (job.get("schedule") or {}).get("expr")),
        ("schedule.tz", promo["tz"], (job.get("schedule") or {}).get("tz")),
        ("payload.argv", declared_argv, (job.get("payload") or {}).get("argv")),
        ("payload.timeoutSeconds", timeout,
         as_seconds((job.get("payload") or {}).get("timeoutSeconds"))),
    ]
    return [(field, declared, live) for field, declared, live in checks if declared != live]


def informational(job):
    payload = job.get("payload") or {}
    found = {key: payload[key] for key in CWD_KEYS if key in payload}
    cwd = next(iter(found.values()), None)
    alert = job.get("failureAlert") or job.get("failure_alert") or {}
    if isinstance(alert, dict) and alert.get("to"):
        alert = dict(alert, to=redact(alert["to"]))
    return {"cwd": cwd, "cwdKey": next(iter(found), None), "failureAlert": alert or None}


def classify(manifest, jobs, only=None):
    promos = manifest["promos"]
    if only:
        wanted = set(only)
        promos = [p for p in promos if p["slug"] in wanted or p["name"] in wanted]
    timeout = manifest["timeoutSeconds"]
    by_name = {job.get("name"): job for job in jobs if job.get("name")}

    report = {"ok": [], "drifted": [], "missing": [], "undeclared": [], "legacy": [], "consoleOwned": [],
              "informational": {}}
    for promo in promos:
        job = by_name.get(promo["name"])
        if job is None:
            report["missing"].append({"name": promo["name"], "cron": promo["cron"],
                                      "tz": promo["tz"]})
            continue
        report["informational"][promo["name"]] = informational(job)
        fields = compare_one(promo, job, timeout)
        entry = {"name": promo["name"], "declared": promo, "live": job}
        if fields:
            entry["fields"] = [{"field": f, "declared": d, "live": l} for f, d, l in fields]
            report["drifted"].append(entry)
        else:
            report["ok"].append(entry)

    # Anything promo-shaped that we did not declare keeps firing forever: register_promo_cron
    # matches on .name only, so a leftover under another name posts the same ad a second time.
    declared_names = {p["name"] for p in manifest["promos"]}
    legacy_owner = {legacy: p["name"] for p in manifest["promos"]
                    for legacy in p.get("legacyNames", [])}
    selected_dirs = {p["name"] for p in promos}
    for job in jobs:
        name = job.get("name")
        if not name or name in declared_names:
            continue
        argv = (job.get("payload") or {}).get("argv") or []
        match = PROMO_ARGV_RE.search(" ".join(str(a) for a in argv))
        if not (name.endswith("-promo") or match):
            continue  # an unrelated gateway job is none of our business
        row = {"name": name, "id": job.get("id"),
               "expr": (job.get("schedule") or {}).get("expr"),
               "tz": (job.get("schedule") or {}).get("tz"),
               "enabled": job.get("enabled"),
               "argv": argv,
               "shadows": match.group(1) if match else None}
        if name not in legacy_owner and console_owned(manifest, name, argv):
            if not only:
                report["consoleOwned"].append(row)
            continue
        if only and not ((match and match.group(1) in selected_dirs) or legacy_owner.get(name) in selected_dirs):
            continue
        if name in legacy_owner:
            row["replacedBy"] = legacy_owner[name]
            report["legacy"].append(row)
        else:
            report["undeclared"].append(row)
    return report


def fmt(expr, tz, timeout):
    return "%-12s %-14s %ss" % (expr, tz, timeout)


def render(report, manifest, stream):
    timeout = manifest["timeoutSeconds"]
    write = stream.write
    write("promo                      declared                       "
          "live                           status\n")
    rows = ([(e, "ok") for e in report["ok"]] + [(e, "DRIFT") for e in report["drifted"]])
    rows.sort(key=lambda pair: [p["name"] for p in manifest["promos"]].index(pair[0]["name"]))
    for entry, status in rows:
        promo, job = entry["declared"], entry["live"]
        schedule = job.get("schedule") or {}
        payload = job.get("payload") or {}
        write("%-26s %s  %s  %s\n" % (
            promo["name"],
            fmt(promo["cron"], promo["tz"], timeout),
            fmt(schedule.get("expr"), schedule.get("tz"),
                as_seconds(payload.get("timeoutSeconds"))),
            status))
    for entry in report["missing"]:
        write("%-26s %s  %-30s %s\n" % (entry["name"], fmt(entry["cron"], entry["tz"], timeout),
                                        "-- no such job --", "MISSING"))

    for entry in report["drifted"]:
        write("\nDRIFT %s\n" % entry["name"])
        for field in entry["fields"]:
            write("  %-24s declared %-28r live %r\n"
                  % (field["field"], field["declared"], field["live"]))
    for row in report["consoleOwned"]:
        write("\nCONSOLE-OWNED %s (id %s): managed by the console campaign reconciler\n"
              % (row["name"], row["id"]))
    for row in report["legacy"] + report["undeclared"]:
        kind = "LEGACY" if "replacedBy" in row else "UNDECLARED"
        write("\n%s %s (id %s)  %r %s, enabled=%s\n"
              % (kind, row["name"], row["id"], row["expr"], row["tz"], row["enabled"]))
        if row["shadows"]:
            write("  argv points at cron/%s/ -- a second job posting the same ad\n"
                  % row["shadows"])
        write("  -> delete it before reinstalling:  openclaw cron rm %s\n" % row["id"])

    info = report["informational"]
    keys = {v["cwdKey"] for v in info.values() if v["cwdKey"]}
    write("\ninformational (not gated): cwd key %s; failureAlert %s\n"
          % (", ".join(sorted(keys)) or "not reported",
             "reported" if any(v["failureAlert"] for v in info.values()) else "not reported"))

    write("\n%d declared: %d ok, %d drifted, %d missing, %d undeclared, %d legacy; %d console-owned\n"
          % (len(manifest["promos"]), len(report["ok"]), len(report["drifted"]),
             len(report["missing"]), len(report["undeclared"]), len(report["legacy"]), len(report["consoleOwned"])))


def main(argv):
    parser = argparse.ArgumentParser(
        description="Compare live gateway cron state to deploy/promo-crons.json.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--live", required=True,
                        help="file holding `openclaw cron list --all --json`, or - for stdin")
    parser.add_argument("--only", action="append", default=[], metavar="PROMO",
                        help="limit to this promo (slug or job name); repeatable")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv[1:])

    try:
        manifest = load_manifest(args.manifest)
    except (OSError, ValueError, json.JSONDecodeError) as ex:
        sys.stderr.write("ERROR: unusable manifest %s: %s\n" % (args.manifest, ex))
        return 2
    if args.only:
        known = {p["slug"] for p in manifest["promos"]} | {p["name"] for p in manifest["promos"]}
        unknown = [name for name in args.only if name not in known]
        if unknown:
            sys.stderr.write("ERROR: unknown promo(s): %s\nknown: %s\n"
                             % (", ".join(unknown),
                                ", ".join(sorted(p["slug"] for p in manifest["promos"]))))
            return 2
    try:
        jobs = load_live(args.live)
    except (OSError, ValueError, json.JSONDecodeError) as ex:
        # Never report "in sync" from input we could not read -- that is the silent-success
        # bug this tool exists to end.
        sys.stderr.write("ERROR: unusable live cron state: %s\n" % ex)
        return 2

    report = classify(manifest, jobs, args.only)
    if args.as_json:
        json.dump({"manifest": args.manifest, "checked": len(report["ok"]) + len(report["drifted"]),
                   "ok": [e["name"] for e in report["ok"]],
                   "drifted": [{"name": e["name"], "fields": e["fields"]}
                               for e in report["drifted"]],
                   "missing": report["missing"], "undeclared": report["undeclared"],
                   "legacy": report["legacy"], "consoleOwned": report["consoleOwned"],
                   "informational": report["informational"]},
                  sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        render(report, manifest, sys.stdout)

    drifted = (report["drifted"] or report["missing"] or report["undeclared"]
               or report["legacy"])
    if drifted and not args.as_json:
        sys.stdout.write("\nremedy: ./deploy/reinstall-promo-crons.sh "
                         "(delete any undeclared/legacy job first)\n")
    return 1 if drifted else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except BrokenPipeError:
        # `--check | head` and `| less` are the obvious ways to read a long report;
        # neither should end in a traceback. Redirect the dangling stdout so the
        # interpreter's own flush at exit cannot raise a second time.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(141)  # 128 + SIGPIPE, the status a shell reports for this
