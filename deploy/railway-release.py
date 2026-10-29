#!/usr/bin/env python3
"""Release only an exact tested commit; roll back the exact previous image on failure."""

import argparse
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
import uuid

PROJECT = "a251f752-3b49-4f72-8a71-4629bf2fced0"
SERVICE = "503baf60-c897-4a77-812a-719195705031"
ENVIRONMENT = "2cae37a4-8e24-41db-87ed-9a3a7c9eff22"
OWNED = ("SOURCE_COMMIT", "GENAIE_CAMPAIGN_TRANSPORT", "GENAIE_CATALOG_IDLE_RETIREMENT",
         "GENAIE_HISTORY_IDLE_RETIREMENT", "MALLOC_ARENA_MAX", "GENAIE_WHATSAPP_CACHE_CLEANUP")
PROFILES = ("preserve", "baseline", "catalog-trial", "history-trial", "allocator-trial", "sender-trial", "cache-trial")
SETTING_FIELDS = {"GENAIE_CAMPAIGN_TRANSPORT": "transport",
                  "GENAIE_CATALOG_IDLE_RETIREMENT": "catalogIdleRetirement",
                  "GENAIE_HISTORY_IDLE_RETIREMENT": "historyIdleRetirement",
                  "MALLOC_ARENA_MAX": "mallocArenaMax",
                  "GENAIE_WHATSAPP_CACHE_CLEANUP": "whatsappCacheCleanup"}
PREFIX = "GENAIE_RELEASE_READY "
SCOPE = ["--project", PROJECT, "--service", SERVICE, "--environment", ENVIRONMENT]


def railway(*arguments, cwd=None):
    result = subprocess.run(["railway", *arguments], cwd=cwd, capture_output=True,
                            text=True, timeout=120)
    if result.returncode:
        # Do not print secret-bearing variable responses or inherited tokens.
        raise RuntimeError("Railway operation failed: " + " ".join(arguments[:2]))
    return json.loads(result.stdout)


def deployments():
    return railway("deployment", "list", *SCOPE, "--limit", "100", "--json")


def wait_for(deployment_id, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            deployment = next((item for item in deployments() if item["id"] == deployment_id), None)
        except RuntimeError:
            deployment = None
        if deployment:
            if deployment["status"] == "SUCCESS":
                return deployment
            if deployment["status"] in {"FAILED", "CRASHED", "REMOVED", "SKIPPED"}:
                raise RuntimeError(f"Deployment {deployment_id} ended with {deployment['status']}")
        time.sleep(5)
    raise TimeoutError(f"Deployment {deployment_id} did not become successful")


def validate_readiness(report, deployment_id, sha, profile, jobs, settings=None):
    if not report.get("ready") or report.get("deploymentId") != deployment_id:
        raise ValueError("release startup verification failed or references another deployment")
    if report.get("sourceCommit") != sha:
        raise ValueError("baked source commit does not match the tested commit")
    expected = ({"transport": "cli", "catalogIdleRetirement": profile} if settings is None else
                {SETTING_FIELDS[key]: value for key, value in settings.items()})
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError("runtime settings do not match the release profile")
    if not {"default", "ads"}.issubset(set(report.get("connectedAccounts", []))):
        raise ValueError("both WhatsApp accounts must be connected")
    if {"vacancy-lamp-feed", "hustlers-feed", "job-scout-group-feed",
        "job-scout-hustlers-feed"}.intersection(report.get("enabledJobs", [])):
        raise ValueError("retired job feeds must not be enabled")
    if not jobs.issubset(set(report.get("enabledJobs", []))):
        raise ValueError("required campaigns are not enabled")


def startup_result(deployment_id, sha, profile, jobs, timeout=600, settings=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["railway", "logs", deployment_id, *SCOPE, "--lines", "100",
                                 "--filter", "GENAIE_RELEASE_READY", "--json"],
                                capture_output=True, text=True, timeout=45)
        if not result.returncode:
            for line in result.stdout.splitlines():
                try:
                    message = json.loads(line).get("message", "")
                    if not message.startswith(PREFIX):
                        continue
                    report = json.loads(message[len(PREFIX):])
                except (ValueError, AttributeError):
                    continue
                validate_readiness(report, deployment_id, sha, profile, jobs, settings)
                return report
        time.sleep(5)
    raise TimeoutError("No validated startup result for the released deployment")


def cancel_pending(deployment_id, timeout=60):
    """Prevent a timed-out build from activating after the rollback."""
    pending = {"INITIALIZING", "QUEUED", "WAITING", "BUILDING", "DEPLOYING"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = next((item for item in deployments() if item["id"] == deployment_id), None)
        if current is None:
            raise RuntimeError("failed candidate is not visible for cancellation")
        if current["status"] not in pending:
            return
        result = railway("api", "mutation($id:String!){deploymentCancel(id:$id)}",
                         "--variables", json.dumps({"id": deployment_id}))
        if result.get("errors"):
            # Cancellation can race with activation. The next scoped read decides.
            time.sleep(2)
            continue
        time.sleep(2)
    raise TimeoutError("failed candidate did not leave its pending state")


def restore(previous, variables):
    present = [f"{key}={value}" for key, value in variables.items() if value is not None]
    if present:
        railway("variable", "set", *present, *SCOPE, "--skip-deploys", "--json")
    for key, value in variables.items():
        if value is None:
            railway("variable", "delete", key, *SCOPE, "--json")
    result = railway("api", "mutation($id:String!){deploymentRedeploy(id:$id,usePreviousImageTag:true){id}}",
                     "--variables", json.dumps({"id": previous["id"]}))
    restored = wait_for(result["data"]["deploymentRedeploy"]["id"])
    expected = previous.get("meta", {}).get("imageDigest")
    if not expected or restored.get("meta", {}).get("imageDigest") != expected:
        raise RuntimeError("rollback image digest does not match the previous successful deployment")
    verify_rollback_startup(restored["id"])
    return {"deploymentId": restored["id"], "imageDigest": expected, "startupVerified": True}


def verify_rollback_startup(deployment_id, timeout=600):
    """Older known images predate the structured startup result; verify their logs too."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["railway", "logs", deployment_id, *SCOPE, "--lines", "200", "--json"],
                                capture_output=True, text=True, timeout=45)
        messages = []
        for line in result.stdout.splitlines():
            try:
                message = json.loads(line).get("message", "")
            except (ValueError, AttributeError):
                continue
            if message.startswith(PREFIX):
                report = json.loads(message[len(PREFIX):])
                if report.get("deploymentId") == deployment_id:
                    if report.get("ready") and {"default", "ads"}.issubset(report.get("connectedAccounts", [])):
                        return
                    raise RuntimeError("rollback startup report is unhealthy")
            messages.append(message)
        if any("[gateway] ready" in message for message in messages) and \
           sum("Listening for WhatsApp inbound messages" in message for message in messages) >= 2 and \
           all(any(f"[{account}] starting provider" in message for message in messages) for account in ["default", "ads"]):
            return
        time.sleep(5)
    raise TimeoutError("rollback image was restored but channel startup could not be verified")


def archive_commit(root, sha, destination):
    archive = subprocess.check_output(["git", "archive", sha], cwd=root)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(destination, filter="data")


def resolve_profile(requested, variables):
    if requested in set(PROFILES) - {"preserve"}:
        return requested
    if requested != "preserve":
        raise ValueError("unknown release profile")
    value = variables.get("GENAIE_CATALOG_IDLE_RETIREMENT")
    if value in (None, "0"):
        return "baseline"
    if value == "1":
        return "catalog-trial"
    raise ValueError("cannot preserve an unrecognized catalog retirement setting")


def resolve_settings(requested, variables):
    defaults = {"GENAIE_CAMPAIGN_TRANSPORT": "cli", "GENAIE_CATALOG_IDLE_RETIREMENT": "0",
                "GENAIE_HISTORY_IDLE_RETIREMENT": "0", "MALLOC_ARENA_MAX": "0",
                "GENAIE_WHATSAPP_CACHE_CLEANUP": "0"}
    if requested in {"baseline", "catalog-trial"}:
        settings = defaults.copy()
        settings["GENAIE_CATALOG_IDLE_RETIREMENT"] = "1" if requested == "catalog-trial" else "0"
        return settings
    if requested not in PROFILES:
        raise ValueError("unknown release profile")
    settings = {key: variables.get(key) or value for key, value in defaults.items()}
    choices = {"GENAIE_CAMPAIGN_TRANSPORT": {"cli", "rpc", "rpc-once"},
               "GENAIE_CATALOG_IDLE_RETIREMENT": {"0", "1"},
               "GENAIE_HISTORY_IDLE_RETIREMENT": {"0", "1"}, "MALLOC_ARENA_MAX": {"0", "2"},
               "GENAIE_WHATSAPP_CACHE_CLEANUP": {"0", "1"}}
    if any(value not in choices[key] for key, value in settings.items()):
        raise ValueError("cannot preserve an unrecognized memory/runtime setting")
    if requested == "history-trial":
        settings["GENAIE_HISTORY_IDLE_RETIREMENT"] = "1"
    elif requested == "allocator-trial":
        settings["MALLOC_ARENA_MAX"] = "2"
    elif requested == "sender-trial":
        settings["GENAIE_CAMPAIGN_TRANSPORT"] = "rpc-once"
    elif requested == "cache-trial":
        settings["GENAIE_WHATSAPP_CACHE_CLEANUP"] = "1"
    return settings


def release(root, sha, profile, report):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("a full commit SHA is required")
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip() != sha:
        raise ValueError("checkout does not match the tested SHA")
    remote = subprocess.check_output(["git", "ls-remote", "origin", "refs/heads/main"], cwd=root,
                                     text=True).split()[0]
    if remote != sha:
        report.update({"status": "superseded", "sourceCommit": sha})
        return
    previous = next((item for item in deployments() if item["status"] == "SUCCESS"), None)
    if not previous or not previous.get("meta", {}).get("imageDigest"):
        raise RuntimeError("no successful deployment with an image digest available for rollback")
    raw_variables = railway("variable", "list", *SCOPE, "--json")
    variables = {key: raw_variables.get(key) for key in OWNED}
    if any(key in raw_variables and raw_variables[key] is None for key in OWNED):
        raise RuntimeError("owned release setting is sealed and cannot safely be restored")
    report["requestedProfile"] = profile
    settings = resolve_settings(profile, variables)
    profile = resolve_profile(profile, variables)
    report.update({"sourceCommit": sha, "profile": profile, "previousDeploymentId": previous["id"],
                   "previousImageDigest": previous.get("meta", {}).get("imageDigest"), "runtimeSettings": settings})
    with tempfile.TemporaryDirectory(prefix="genaebot-release-") as temporary:
        archive_commit(root, sha, temporary)
        manifest = json.loads((Path(temporary) / "deploy/promo-crons.json").read_text())
        entries = manifest if isinstance(manifest, list) else manifest.get("promos", manifest.get("jobs", []))
        # The repository's manifest is the authority; never install or edit crons here.
        jobs = {item["name"] for item in entries}
        idle = settings["GENAIE_CATALOG_IDLE_RETIREMENT"]
        identifier = None
        upload_started = False
        release_message = f"commit:{sha} profile:{profile} release:{uuid.uuid4().hex}"
        report["releaseMessage"] = release_message
        try:
            railway("variable", "set", f"SOURCE_COMMIT={sha}",
                    *(f"{key}={value}" for key, value in settings.items()),
                    *SCOPE, "--skip-deploys", "--json")
            upload_started = True
            result = railway("up", *SCOPE, "--detach", "--json", "--message",
                             release_message, cwd=temporary)
            identifier = result["deploymentId"]
            report["deploymentId"] = identifier
            deployed = wait_for(identifier)
            report["imageDigest"] = deployed.get("meta", {}).get("imageDigest")
            if not report["imageDigest"]:
                raise RuntimeError("released image digest is missing")
            report["startup"] = startup_result(identifier, sha, idle, jobs, settings=settings)
            report["status"] = "healthy"
        except Exception:
            report["status"] = "failed"
            if upload_started:
                try:
                    if not identifier:
                        candidate = next((item for item in deployments()
                                          if item.get("meta", {}).get("cliMessage") == release_message), None)
                        if not candidate:
                            raise RuntimeError("upload returned no deployment ID; inspect the unique release message for a late candidate")
                        identifier = candidate["id"]
                        report["deploymentId"] = identifier
                    cancel_pending(identifier)
                except Exception as error:
                    report["candidateCancellationError"] = str(error)
            try:
                report["rollback"] = restore(previous, variables)
            except Exception as error:
                report["rollbackError"] = str(error)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--profile", choices=PROFILES, default="preserve")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"status": "failed", "sourceCommit": args.sha}
    try:
        release(Path(__file__).resolve().parent.parent, args.sha, args.profile, report)
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ["status", "sourceCommit", "deploymentId"] if key in report}))


if __name__ == "__main__":
    main()
