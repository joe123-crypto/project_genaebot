#!/usr/bin/env bash
# Print the live campaign list, as JSON, by asking the gateway.
#
# The schedules, target counts and job names used to be committed -- in
# deploy/promo-crons.json and each campaign's targets.json -- so the release
# gates could read them straight out of the checkout. They are operator data and
# now live in Firestore, which CI has no credentials for, so the gates ask the
# gateway instead: it already reads that config for every run, and the deploy job
# already holds a RAILWAY_TOKEN.
#
# The access mechanism itself is not repeated here; deploy/gateway-exec.sh owns
# it, and sourcing it is the whole point of this wrapper existing.
#
# Usage: deploy/gateway-campaigns.sh            # the `campaigns` read, verbatim
# Read by deploy/gateway_campaigns.py, which is what the gates import.

set -euo pipefail

# shellcheck source=deploy/gateway-exec.sh disable=SC1091 # computed path, not followed
source "$(dirname "${BASH_SOURCE[0]}")/gateway-exec.sh"

# console-read lives on the volume with the rest of the deployed workspace, runs
# as joseph like every other workspace command (root trips the plugins' own
# ownership check), and needs the gateway's environment for its Firestore
# credentials -- so OPENCLAW_HOME is dropped and HOME is set exactly as the
# openclaw wrapper does, and nothing else is touched.
exec "${GATEWAY_EXEC[@]}" runuser -u joseph -- sh -lc '
  exec env -u OPENCLAW_HOME HOME=/home/joseph \
    "${OPENCLAW_STATE_DIR:-/home/joseph/.openclaw}/workspace/bin/console-read" campaigns
'
