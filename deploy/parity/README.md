# Job Scout parity, cutover evidence, and rollback validation

This directory contains Workstream 6 local tooling. It does not pull artifacts,
call GitHub, query OpenClaw, deploy, restart containers, alter cron jobs, remove
source, or write below a runtime root. Production operators supply already
extracted overlays and already captured JSON inputs.

## Frozen manifest contract

The only accepted `schemaVersion: 1` shape is the Workstream 3 contract committed
at `9459a9b`:

- top-level `paths`, never `files`;
- `mode` is exactly the string `0644` or `0755`, and `executable` must agree;
- `images` contains structured `browser` and `channel-listener` objects with
  digest-pinned references;
- repository URL, commit, protocol, required live paths, overlay root, metadata,
  and sorted path order are validated exactly.

The current Workstream 4 draft uses `files`, integer modes, and string image
values. That consumer is incompatible with the frozen producer contract and is
an unmet integration gate. This tooling deliberately has no compatibility
parser for the WS4 shape.

## Local parity comparison

Extract a WS3 bundle into a disposable directory. Build the current curated
stage into a different disposable directory, then run:

```bash
python3 deploy/parity/job_scout_cutover.py compare \
  --bundle-root "$BUNDLE_ROOT" \
  --curated-root "$CURATED_STAGE" \
  --manifest "$BUNDLE_ROOT/job-scout-manifest.json" \
  --output "$EVIDENCE_DIR/parity.json"
```

The comparison requires the manifest, extracted bundle, and current curated
ownership set to have the same files. It verifies each exact relative path,
SHA-256, byte count, `0644`/`0755` mode, and executable flag. Extra files or
directories, missing files, symlinks, traversal, ownership violations, and
runtime/private/cache paths are rejected.

On-disk permissions are not collapsed to an executable boolean. Modes such as
`0600`, `0664`, `0700`, `0775`, or `0777` fail even when their executable bit
would otherwise agree. The composed stage must normalize owned files to WS3's
canonical `0644`/`0755` modes before parity can pass.

The overlay allowlist mirrors WS3. Worker Dockerfiles are not overlay files;
they remain in the post-removal source guard because `genaebot` must stop owning
those build inputs after cutover.

## Runtime evidence

Capture the common runtime parent before and after an approved cutover or local
rehearsal:

```bash
python3 deploy/parity/job_scout_cutover.py snapshot-runtime \
  --root "$GENAEBOT_RUNTIME_ROOT" \
  --output "$EVIDENCE_DIR/runtime-before.json"

python3 deploy/parity/job_scout_cutover.py snapshot-runtime \
  --root "$GENAEBOT_RUNTIME_ROOT" \
  --output "$EVIDENCE_DIR/runtime-after.json"
```

The command only reads the tree. Each snapshot records the runtime root and the
required `job-scout`, `job-scout-channel`, listener auth, and listener media
directory identities: path, device, inode, mode, UID, and GID. Those identities
and ownership values must remain exact. Replacing, relocating, deleting,
re-permissioning, or changing ownership of a required runtime directory fails.

Content digest, entry/file/directory counts, and total bytes are stored as
before/after observations. They may differ because SQLite, listener health, and
application state can legitimately advance while services remain live. No
runtime file names or contents enter evidence. The two snapshot inputs must be
distinct files, their `capturedAt` timestamps must be strictly ordered, and the
declared deployment time must fall between them.

Snapshot output, rollback records, and rollback-selection output are rejected if
placed below the recorded runtime root. The tooling has no operation that
writes, moves, or removes runtime content.

## Composite release record

Supply an OpenClaw `cron list --all --json` snapshot containing the five enabled
Job Scout jobs. `record` verifies their cadence, timezone, isolated command
shape, payload kind, working directory, timeout, and full failure-alert shape
(`after: 1`, WhatsApp, a non-empty recipient, and `1h` cooldown), then archives
the manifest and extracted overlay under a local records directory:

```bash
python3 deploy/parity/job_scout_cutover.py record \
  --bundle-root "$BUNDLE_ROOT" \
  --curated-root "$CURATED_STAGE" \
  --manifest "$BUNDLE_ROOT/job-scout-manifest.json" \
  --runtime-before "$EVIDENCE_DIR/runtime-before.json" \
  --runtime-after "$EVIDENCE_DIR/runtime-after.json" \
  --cron-snapshot "$EVIDENCE_DIR/openclaw-crons.json" \
  --health-evidence "$EVIDENCE_DIR/production-health.json" \
  --records-dir "$ROLLBACK_RECORDS" \
  --genaebot-sha "$GENAEBOT_SHA" \
  --job-scout-sha "$JOB_SCOUT_SHA" \
  --source-bundle-digest "$BUNDLE_DIGEST" \
  --browser-digest "$BROWSER_DIGEST" \
  --listener-digest "$LISTENER_DIGEST" \
  --deployment-time "$DEPLOYMENT_TIME" \
  --job-scout-workflow-run-id "$JOB_SCOUT_RUN_ID" \
  --genaebot-workflow-run-id "$GENAEBOT_RUN_ID" \
  --correlation-id "$CORRELATION_ID" \
  --deployment-status local-rehearsal
```

The composite record contains both repository SHAs, all three artifact digests,
deployment time, both workflow run IDs, correlation ID, five exact cron objects,
parity results, stable runtime identity/topology, before/after content
observations, and health evidence. Browser and listener digests must equal the
structured manifest image digests.

`production-success` requires this exact health-evidence shape, with every check
passing and observed after the declared deployment time:

```json
{
  "schemaVersion": 1,
  "kind": "job-scout-production-health",
  "capturedAt": "2026-08-01T10:02:00Z",
  "checks": {
    "gateway": {"passed": true, "observedAt": "2026-08-01T10:01:00Z", "summary": "gateway healthy"},
    "browser": {"passed": true, "observedAt": "2026-08-01T10:01:00Z", "summary": "browser ready"},
    "channel": {"passed": true, "observedAt": "2026-08-01T10:01:00Z", "summary": "channel healthy"},
    "runtime": {"passed": true, "observedAt": "2026-08-01T10:01:00Z", "summary": "runtime checks passed"},
    "publicStatus": {"passed": true, "observedAt": "2026-08-01T10:01:00Z", "summary": "status/onboarding preview passed"},
    "r2CvAccess": {"passed": true, "observedAt": "2026-08-01T10:01:00Z", "summary": "R2 CV access passed"}
  }
}
```

The check set is closed. Every entry requires a boolean `passed`, timezone-aware
`observedAt`, and non-empty `summary`; observations must fall between deployment
and `capturedAt`. A missing or failed check blocks rollback eligibility. A
`local-rehearsal` record may omit health evidence and remains ineligible for
rollback.

## Rollback selection

Rollback validation produces a selection document and performs no deployment:

```bash
python3 deploy/parity/job_scout_cutover.py validate-rollback \
  --records-dir "$ROLLBACK_RECORDS" \
  --current-record-id "$CURRENT_RECORD_ID" \
  --manifest "$ROLLBACK_RECORDS/$PRIOR_RECORD_ID/release-manifest.json" \
  --output "$EVIDENCE_DIR/rollback-selection.json"
```

The selected manifest must be the exact catalog path and hash of an earlier
`production-success` record. A copied manifest, an arbitrary same-content file,
a local rehearsal, the current record, a later record, a modified archived
overlay, modified evidence, or a legacy production record without passing
health evidence is rejected. Health evidence is revalidated for both the current
and selected records. The selection always carries
`runtimePolicy: preserve`; a separate integrated deployment implementation must
apply the recorded overlay and image digests without touching runtime.

## Source-reintroduction guard

`test_job_scout_source_guard.py` is automatically discovered by the existing
portable CI test runner. While this file is absent, the repository-level guard
is disabled:

```text
deploy/parity/job-scout-source-removal.json
```

Only the reviewed source-removal change after a successful artifact-backed
deployment may add it:

```json
{
  "schemaVersion": 1,
  "enabled": true,
  "cutoverRecordId": "record-000001-<job-scout-sha-prefix>"
}
```

Once present, CI rejects reintroduction of the Job Scout implementation,
wrappers, skill, onboarding document, browser Dockerfile, or channel-listener
Dockerfile. An activation file with `enabled: false` is invalid; disabling the
guard requires a reviewed removal of the activation file.

## Deferred production evidence

The following remain hard gates and are not performed by this workstream branch:

- integrate the standalone repository and WS3 artifact outputs;
- adapt WS4 to the frozen WS3 manifest schema and pass consumer rejection tests;
- integrate WS5 dispatch, receiver, polling, and recorded-manifest rollback flow;
- publish and deploy the bootstrap artifact from the exact deployed source;
- capture public status/onboarding preview, browser readiness, channel health,
  R2 CV accessibility, and all five live cron definitions;
- prove one artifact-backed deployment and rollback while runtime roots,
  topology, mode, and ownership remain stable;
- remove duplicate source and add the activation marker;
- enable automatic `main` release deployment after all preceding gates pass.

No live all-user dispatch, document resend, application submission, external
source-credit spend, production deploy, cutover, rollback, or source removal is
part of the local validation described here.
