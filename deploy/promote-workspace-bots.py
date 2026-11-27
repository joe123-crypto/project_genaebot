#!/usr/bin/env python3
"""Promote a released commit to every workspace's bot.

The gateway is the canary. This runs only after ``deploy/railway-release.py``
has released this exact commit and the gateway passed its startup check -- a
failed release rolls the gateway back and fails the deploy job, so this never
runs for one. It does two things and no more:

1. waits for ``publish-bot-image.yml`` to have pushed this commit's image to
   GHCR (the two workflows run side by side, and a workspace's bot is created
   from the published image, not from uploaded source); then
2. tells the operator console that this image is the fleet's new target.

It deploys nothing itself. The console rolls the image out one bot at a time,
each in its own quiet window, checks it, and rolls it back and stops if it does
not come up -- see genaiebot-console's ``docs/provision-bot.md``.

Until the console is set up to receive it (``CONSOLE_URL`` and
``FLEET_RELEASE_TOKEN``) this warns and succeeds: the gateway's release is not
failed for a step that is switched off. Once it is, a failure is a failure.

Std-lib only, like everything else the deploy job runs.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

REGISTRY = "ghcr.io"
ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
TIMEOUT_SECONDS = 20


def repository(owner):
    # GHCR names are lower case; publish-bot-image.yml lowers the owner too.
    return "%s/genaebot" % owner.lower()


def image_ref(owner, sha):
    return "%s/%s:%s" % (REGISTRY, repository(owner), sha)


def _open(request, opener):
    return opener(request, timeout=TIMEOUT_SECONDS)


def published(owner, sha, opener=urllib.request.urlopen):
    """Whether this commit's image is on GHCR. The package is public, so an
    anonymous pull token is enough to ask."""
    name = repository(owner)
    with _open(urllib.request.Request(
            "https://%s/token?scope=repository:%s:pull" % (REGISTRY, name)), opener) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        "https://%s/v2/%s/manifests/%s" % (REGISTRY, name, sha), method="HEAD",
        headers={"Authorization": "Bearer %s" % token, "Accept": ACCEPT})
    try:
        with _open(request, opener) as response:
            return response.status == 200
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def promote(console_url, token, image, opener=urllib.request.urlopen):
    request = urllib.request.Request(
        console_url.rstrip("/") + "/api/internal/fleet/release", method="POST",
        data=json.dumps({"image": image}).encode("utf-8"),
        headers={"Authorization": "Bearer %s" % token, "Content-Type": "application/json",
                 "Accept": "application/json"})
    with _open(request, opener) as response:
        return json.load(response)


def main(argv=None, env=None, opener=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--sha", required=True)
    parser.add_argument("--owner", required=True, help="the repository owner the image is published under")
    parser.add_argument("--wait-minutes", type=float, default=25)
    parser.add_argument("--poll-seconds", type=float, default=30)
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        print("::error::a full commit SHA is required", file=sys.stderr)
        return 2
    console_url = (env.get("CONSOLE_URL") or "").strip()
    token = (env.get("FLEET_RELEASE_TOKEN") or "").strip()
    if not console_url or not token:
        print("::warning::CONSOLE_URL or FLEET_RELEASE_TOKEN is not set, so workspace bots "
              "were not told about this release. They keep the image they have.")
        return 0

    image = image_ref(args.owner, args.sha)
    deadline = clock() + args.wait_minutes * 60
    while True:
        try:
            if published(args.owner, args.sha, opener=opener):
                break
        except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
            print("GHCR could not be asked yet (%s); trying again." % error)
        if clock() >= deadline:
            print("::error::%s was not published within %g minutes, so workspace bots were not "
                  "promoted. Re-run this job once publish-bot-image has finished."
                  % (image, args.wait_minutes), file=sys.stderr)
            return 1
        sleep(args.poll_seconds)

    try:
        answer = promote(console_url, token, image, opener=opener)
    except urllib.error.HTTPError as error:
        # The console's own curated words, never the token.
        try:
            detail = json.load(error).get("error") or error.reason
        except (ValueError, AttributeError):
            detail = error.reason
        print("::error::the console refused %s (%s): %s" % (image, error.code, detail), file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as error:
        print("::error::the console could not be reached: %s" % error, file=sys.stderr)
        return 1
    print("Promoted %s to workspace bots: %s" % (image, json.dumps(answer, sort_keys=True)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
