"""Which operator config a campaign run acts on: the repo's files, or Firestore.

One variable decides, and this module is the only thing that reads it::

    GENAIE_CONFIG_SOURCE   unset or "file"   membership.json + the generated
                                             targets.json, as it has always been
                           "shadow"          the same, plus a background
                                             comparison (see :mod:`config_shadow`)
                           "firestore"       Firestore decides where ads go

:func:`resolve` answers the question once per run and hands back everything the
run needs: the targets payload, the membership the caps derive from, and each
campaign's on/off switch. Resolving once matters beyond the cutover -- the engine
used to open the targets file five times, so a config change landing mid-run
could split one run across two configs.

The two sources are deliberately asymmetric, because their failure modes are:

* **The file cannot fail closed.** It ships inside the image, so if it is absent
  something is wrong with the build, not with the data. That path keeps its old
  behaviour exactly, down to the stderr wording, so flipping the variable back is
  a true rollback.
* **Firestore fails closed.** An outage serves the last-known-good snapshot; too
  old a snapshot, or none, and :func:`resolve` raises
  :class:`operator_config.ConfigUnavailable` and the caller must send nothing.
  membership.json is never a fallback here: once Firestore is canonical the file
  is stale targeting, and falling back to it would re-post into a group someone
  had just paused.

Std-lib only, so it stays importable from the OpenClaw ``--command`` cron
scripts. ``promo_membership`` is imported lazily: it imports ``promo_broadcast``,
which imports this module.
"""

import json
import datetime
import time
from zoneinfo import ZoneInfo
import os
import sys

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import operator_config  # noqa: E402

MODE_VAR = "GENAIE_CONFIG_SOURCE"
FILE, SHADOW, FIRESTORE = "file", "shadow", "firestore"
MODES = (FILE, SHADOW, FIRESTORE)

# The cron manifest, where each campaign's on/off switch is declared while the
# file is the source. The image bakes a copy outside deploy/, which is never
# staged into the container.
WORKSPACE = os.path.dirname(LIB)
MANIFEST_CANDIDATES = (
    os.path.join(os.path.dirname(WORKSPACE), "deploy", "promo-crons.json"),
    "/usr/local/lib/genaie/promo-crons.json",
)
PROMO_DIR_SUFFIX = "-promo"
PREFIX = "[operator-source]"


def mode(env=None):
    """``(mode, warning)``: one of :data:`MODES`, plus a message when the
    variable holds something this code does not know. An unreadable setting must
    not silently change where ads go, so it reads as ``file``."""
    env = os.environ if env is None else env
    raw = (env.get(MODE_VAR) or "").strip().lower()
    if raw == "":
        return FILE, None
    if raw in MODES:
        return raw, None
    return FILE, ("%s=%r is not one of %s; acting on membership.json"
                  % (MODE_VAR, env.get(MODE_VAR), ", ".join(MODES)))


def promo_key(promo_name):
    """``"cv-promo"`` -> ``"cv"``: the key membership.json files a promo under."""
    name = str(promo_name or "")
    if name.endswith(PROMO_DIR_SUFFIX):
        return name[:-len(PROMO_DIR_SUFFIX)]
    return name


def manifest_paths(env=None):
    env = os.environ if env is None else env
    override = env.get("GENAIE_PROMO_CRONS")
    return (override,) if override else MANIFEST_CANDIDATES


def read_manifest(env=None):
    """The cron manifest as a dict, or ``None`` when no candidate exists.

    Raises :class:`operator_config.ConfigInvalid` for a manifest that is present
    but unparseable -- a typo in the switches must be reported, not read as
    "nothing is switched off"."""
    for path in manifest_paths(env):
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            continue
        except ValueError as ex:
            raise operator_config.ConfigInvalid("%s is not valid JSON: %s" % (path, ex))
    return None


def manifest_switches(env=None):
    """``{slug: bool}`` from the cron manifest. Fail-open: no manifest at all
    yields ``{}``, which reads as "nothing known to be off" -- the behaviour
    :func:`promo_caps.disabled_promos` has always had."""
    try:
        manifest = read_manifest(env)
        if manifest is None:
            return {}
        return operator_config.manifest_switches(manifest)
    except operator_config.ConfigInvalid:
        return {}


def targets_doc_from_file(targets_file, err=None):
    """``(doc, read)``: the targets file as a dict, and whether it could be read.
    Keeps the wording the engine has always printed, so a rollback reads
    identically in the run logs."""
    err = err or sys.stderr
    try:
        with open(targets_file, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        err.write("ERROR: targets file not found: %s\n" % targets_file)
        return {}, False
    except json.JSONDecodeError as ex:
        err.write("ERROR: targets file is not valid JSON: %s\n" % ex)
        return {}, False
    except OSError as ex:
        err.write("ERROR: targets file unreadable: %s\n" % ex)
        return {}, False
    if not isinstance(data, dict):
        # A bare list is still a valid group list; the engine has always taken one.
        return {"groups": data}, True
    return data, True


def resolve(promo, targets_file=None, membership_path=None, env=None, err=None,
            config_loader=None, **inject):
    """What this run acts on::

        {"source": "file" | "firestore" | "snapshot",
         "mode": "file" | "shadow" | "firestore",
         "doc": <targets.json-shaped dict>,
         "membership": <membership-shaped dict> | None,
         "switches": {slug: bool},         # on only if BOTH switches are on
         "storeSwitches": {slug: bool},    # the operator's switch alone (Firestore)
         "cronSwitches": {slug: bool},     # the manifest alone: is a cron registered
         "enabled": True | False | None,   # this promo's own switch in the store
                                           # (what gates the run); None = unknown
         "report": {...},                  # quarantine/warnings, firestore only
         "notes": [str]}

    ``promo`` may be the campaign slug or the ``<slug>-promo`` job name. Raises
    :class:`operator_config.ConfigUnavailable` in ``firestore`` mode when there
    is no trustworthy config, and nothing else: in ``file`` mode an unreadable
    file yields an empty doc, exactly as before, and the caller stays silent."""
    import promo_membership  # deferred: promo_membership imports promo_broadcast

    env = os.environ if env is None else env
    err = err or sys.stderr
    slug = promo_key(promo)
    current, warning = mode(env)
    import campaign_deletion
    if campaign_deletion.enabled(env) and current != FIRESTORE:
        raise operator_config.ConfigUnavailable("campaign deletion protection requires live Firestore configuration")
    notes = []

    def note(message):
        notes.append(message)
        err.write("%s %s\n" % (PREFIX, message))

    if warning:
        note(warning)

    if current == FIRESTORE:
        if config_loader is None and slug:
            loaded = operator_config.load(env, log=note, campaign=slug, **inject)
        else:
            loaded = (config_loader or operator_config.load)(env, log=note, **inject)
        membership = loaded["membership"]
        # Two switches, and a campaign is on only if both are: the cron manifest
        # decides whether the gateway has a cron registered at all, Firestore
        # decides whether a run that fires sends anything. ``switches`` combines
        # them, so the caps never hand a group's allowance to a campaign that
        # cannot post either way. ``enabled`` stays the Firestore switch alone:
        # it is the operator's live pause control, and it is what gates this run.
        declared = manifest_switches(env)
        switches = dict((slug_, on and declared.get(slug_, True))
                        for slug_, on in loaded["enabled"].items())
        # Data-defined campaigns have no baked manifest entry. A pending job
        # cannot consume cap turns until reconciliation verifies its schedule.
        for key, meta in (membership.get("promos") or {}).items():
            if meta.get("kind") == "console":
                import console_campaigns
                campaign_day = datetime.datetime.fromtimestamp(inject.get("now", time.time)(), ZoneInfo("Africa/Harare")).date()
                switches[key] = (switches.get(key, False) and meta.get("scheduleStatus") == "ready"
                                 and console_campaigns.schedule_occurrence(meta, campaign_day) is not None)
        # A caller that names no campaign (the console's read model) wants the
        # membership and the switches, not one campaign's target list.
        doc = promo_membership.targets_payload(membership, slug) if slug else {}
        if loaded["degraded"]:
            err.write("%s serving the snapshot taken %s: %s\n"
                      % (PREFIX, loaded.get("fetchedAt"), loaded["degraded"]))
        return {"source": loaded["source"], "mode": current, "doc": doc,
                "membership": membership, "switches": switches,
                "storeSwitches": dict(loaded["enabled"]), "cronSwitches": declared,
                "enabled": loaded["enabled"].get(slug), "report": loaded["report"], "deleted": loaded.get("deleted", []),
                "notes": notes}

    doc = {}
    if targets_file:
        doc, _ = targets_doc_from_file(targets_file, err=err)
    try:
        membership = promo_membership.load(membership_path)
    except ValueError as ex:
        membership = None
        warning = ("WARN: per-group caps not applied (%s: %s); sending to "
                   "every eligible group." % (type(ex).__name__, ex))
        notes.append(warning)
        err.write(warning + "\n")
    switches = manifest_switches(env)
    # In file mode the manifest is the only switch, so it is both of them.
    return {"source": FILE, "mode": current, "doc": doc, "membership": membership,
            "switches": switches, "storeSwitches": dict(switches),
            "cronSwitches": dict(switches), "enabled": switches.get(slug),
            "report": {"quarantined": {}, "warnings": [], "campaigns": {}},
            "notes": notes}
