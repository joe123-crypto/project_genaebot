#!/usr/bin/env python3
"""Force the tracked WhatsApp reply gates into the live openclaw.json.

Nothing else on the Railway boot path applies config/openclaw.template.json (see
README), so a hand-edit to the live config on the volume silently outlives every
deploy. On 2026-10-18 the reply gates were turned off that way during a Railway
outage and the repo held no trace of it: channels.whatsapp.dmPolicy had been set
to "allowlist" with only the admin number in channels.whatsapp.allowFrom, so the
bot answered the operator and nobody else.

This sets reply policy gates to their template values on every boot. Default
account enablement is seeded only when absent; an operator pause survives boot.
The ads account's DM policy is disabled for the broadcast-only trial; its
listener remains enabled so scheduled group advertising can continue.
The two reply-timing settings (typing mode, WhatsApp inbound debounce) ride along
for the same reason: nothing else carries a template change to the live config.
Only scalar leaves are written -- no object or array is ever replaced -- so the
live-only plugin keys that openclaw.plugin.json still requires survive untouched
(replacing a config subtree is what crashlooped production on 2026-10-12).

The one exception is channels.whatsapp.allowFrom: dmPolicy="open" drops every DM
unless "*" is present, so "*" is APPENDED when missing. allowFrom holds admin and
public phone numbers, so it is never replaced or reordered.

To turn replies off, set the Railway variable REPLIES_DISABLED=1. That is enforced
here on every boot, so the off-state lives in the dashboard rather than in an
untraceable edit to the volume.

Usage: sync-reply-gates.py TEMPLATE LIVE_CONFIG [--applier PATH] [--dry-run]

Never fatal: any problem is reported on stderr and the config is left as it was,
so a bad template cannot stop the gateway from starting.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_APPLIER = Path(__file__).resolve().parent.parent / "patch-openclaw-config.py"

# Scalar reply gates, each forced to its config/openclaw.template.json value.
GATED_PATHS: list[list[str]] = [
    ["channels", "whatsapp", "enabled"],
    ["channels", "whatsapp", "dmPolicy"],
    ["channels", "whatsapp", "accounts", "ads", "dmPolicy"],
    ["channels", "whatsapp", "accounts", "default", "enabled"],
    ["plugins", "entries", "whatsapp", "enabled"],
    ["plugins", "entries", "public-service-gate", "enabled"],
    ["plugins", "entries", "public-service-gate", "config", "enabled"],
    ["plugins", "entries", "whatsapp-relevance-gate", "config", "enabled"],
    ["plugins", "entries", "whatsapp-relevance-gate", "config", "relevanceMode"],
    # Where CV buyers are told to pay. Governed so a payee change in the template
    # reaches the live bot on the next deploy instead of an old number lingering.
    ["plugins", "entries", "public-service-gate", "config", "cvMakerPaymentDetails"],
    # How a reply is timed, not whether it is sent: "typing" shows when the reply
    # text starts rather than the instant a message lands, and a burst of short
    # messages is answered once. Governed so the pacing in
    # public-service-gate/lib/reply-pacing.js is not undone by a stale live value.
    ["agents", "defaults", "typingMode"],
    ["messages", "inbound", "byChannel", "whatsapp"],
]

# Paths this script must never write, each for a reason a later edit could forget.
NEVER_WRITE: frozenset[str] = frozenset(
    {
        # The template's two entries omit the admin->main routes that
        # patch-openclaw-config.py generates from whatsappAdminUsers at apply
        # time, so forcing this would delete admin routing. Checked, not written.
        "bindings",
        # Group replies are hard-blocked in public-service-gate/index.js and this
        # script does not govern group access.
        "channels.whatsapp.groupPolicy",
        # Listener enablement is operator-controlled. Disabling the listener
        # would also stop advertising, unlike the governed ads DM policy.
        "channels.whatsapp.accounts.ads.enabled",
    }
)

DMPOLICY_PATH = ["channels", "whatsapp", "dmPolicy"]
ALLOWFROM_PATH = ["channels", "whatsapp", "allowFrom"]
DEFAULT_ACCOUNT_ENABLED = ["channels", "whatsapp", "accounts", "default", "enabled"]
ADS_DM_POLICY = ["channels", "whatsapp", "accounts", "ads", "dmPolicy"]
ENV_KILL_SWITCH = "REPLIES_DISABLED"


def load_applier(applier: Path):
    spec = importlib.util.spec_from_file_location("patch_openclaw_config", applier)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {applier}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def truthy(raw: str | None) -> bool:
    """Railway variables arrive as text and sometimes keep their quotes."""
    if raw is None:
        return False
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value.lower() in {"1", "true", "yes", "on"}


def dotted(path: list[str]) -> str:
    return ".".join(path)


def covers(outer: list[str], inner: list[str]) -> bool:
    """True when outer is inner, or an ancestor of it."""
    return len(outer) <= len(inner) and inner[: len(outer)] == outer


def render(found: bool, value) -> str:
    return json.dumps(value) if found else "<absent>"


def check_invariants(template: dict, preserve_paths: list[list[str]], get_path) -> None:
    for path in GATED_PATHS:
        name = dotted(path)
        for blocked in NEVER_WRITE:
            parts = blocked.split(".")
            if covers(parts, path) or covers(path, parts):
                raise ValueError(f"{name} collides with the NEVER_WRITE entry {blocked}")
        for preserve in preserve_paths:
            if covers(preserve, path) or covers(path, preserve):
                raise ValueError(f"{name} overlaps the PRESERVE_PATHS entry {dotted(preserve)}")
        found, value = get_path(template, path)
        if not found:
            raise ValueError(f"{name} is absent from the template")
        if value is None or isinstance(value, (dict, list)):
            raise ValueError(f"{name} is not a scalar in the template; this writes leaves only")


def plan_changes(live: dict, template: dict, get_path) -> list[tuple[list[str], bool, object, object]]:
    changes = []
    for path in GATED_PATHS:
        _, want = get_path(template, path)
        found, have = get_path(live, path)
        if path == DEFAULT_ACCOUNT_ENABLED and found and isinstance(have, bool):
            continue
        if found and type(have) is type(want) and have == want:
            continue
        changes.append((path, found, have, want))
    return changes


def ensure_wildcard_allowfrom(live: dict, get_path, set_path) -> str | None:
    """dmPolicy="open" drops every DM unless allowFrom carries "*". Append it."""
    found, policy = get_path(live, DMPOLICY_PATH)
    if not found or policy != "open":
        return None
    found, allow = get_path(live, ALLOWFROM_PATH)
    if not found or allow is None:
        set_path(live, ALLOWFROM_PATH, ["*"])
        return 'created channels.whatsapp.allowFrom as ["*"]'
    if not isinstance(allow, list):
        raise ValueError("channels.whatsapp.allowFrom is not an array; refusing to touch it")
    if "*" in allow:
        return None
    before = len(allow)
    allow.append("*")
    return f'appended "*" to channels.whatsapp.allowFrom ({before} entries -> {before + 1})'


def public_binding_warning(live: dict) -> str | None:
    """bindings is never written; a missing public route is reported instead."""
    bindings = live.get("bindings")
    if not isinstance(bindings, list):
        return "the live config has no bindings array"
    for entry in bindings:
        if not isinstance(entry, dict) or entry.get("agentId") != "public":
            continue
        match = entry.get("match")
        if not isinstance(match, dict) or match.get("channel") != "whatsapp":
            continue
        peer = match.get("peer")
        if isinstance(peer, dict) and peer.get("kind") == "direct":
            return None
    return "no binding routes direct WhatsApp peers to the public agent"


def write_config(config_path: Path, config: dict) -> None:
    stat = config_path.stat()
    fd, tmp = tempfile.mkstemp(dir=config_path.parent, prefix=f".{config_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.chmod(tmp, stat.st_mode & 0o7777)
        try:
            os.chown(tmp, stat.st_uid, stat.st_gid)
        except PermissionError:
            pass
        os.replace(tmp, config_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def sync(
    template_path: Path,
    config_path: Path,
    applier: Path = DEFAULT_APPLIER,
    dry_run: bool = False,
) -> tuple[str, list[str]]:
    """Returns ("updated"|"unchanged", log lines). Raises on any problem."""
    module = load_applier(applier)
    get_path, set_path = module.get_path, module.set_path

    template = json.loads(template_path.read_text(encoding="utf-8"))
    if not isinstance(template, dict):
        raise ValueError(f"{template_path} is not a JSON object")
    check_invariants(template, module.PRESERVE_PATHS, get_path)

    live = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(live, dict):
        raise ValueError(f"{config_path} is not a JSON object")

    lines: list[str] = []
    warning = public_binding_warning(live)
    if warning:
        lines.append(f"reply gates WARNING: {warning}")

    changed: list[str] = []
    if truthy(os.environ.get(ENV_KILL_SWITCH)):
        # The existing kill switch controls default listener enablement. It
        # must not bypass the independent ads broadcast-only policy on boot.
        for path, found, have, want in plan_changes(live, template, get_path):
            if path != ADS_DM_POLICY:
                continue
            set_path(live, path, want)
            note = f"{dotted(path)} {render(found, have)} -> {json.dumps(want)}"
            changed.append(note)
            lines.append("reply gates: " + note)
        found, have = get_path(live, DEFAULT_ACCOUNT_ENABLED)
        if found and have is False:
            lines.append(f"reply gates: {ENV_KILL_SWITCH} set; replies already off")
        else:
            set_path(live, DEFAULT_ACCOUNT_ENABLED, False)
            note = f"{dotted(DEFAULT_ACCOUNT_ENABLED)} {render(found, have)} -> false"
            changed.append(note)
            lines.append(
                f"reply gates: {ENV_KILL_SWITCH} set; {note} (replies off, by operator variable)"
            )
    else:
        for path, found, have, want in plan_changes(live, template, get_path):
            set_path(live, path, want)
            changed.append(f"{dotted(path)} {render(found, have)} -> {json.dumps(want)}")
        if changed:
            lines.append("reply gates updated: " + "; ".join(changed))
        appended = ensure_wildcard_allowfrom(live, get_path, set_path)
        if appended:
            changed.append(appended)
            lines.append(f"reply gates: {appended}")

    if not changed:
        lines.append(f"reply gates unchanged ({len(GATED_PATHS)} paths already match the template)")
        return "unchanged", lines
    if dry_run:
        lines.append("reply gates: DRY RUN, nothing written")
        return "unchanged", lines
    write_config(config_path, live)
    return "updated", lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Force the tracked WhatsApp reply gates into the live openclaw.json."
    )
    parser.add_argument("template", help="config/openclaw.template.json")
    parser.add_argument("config", help="the live openclaw.json")
    parser.add_argument("--applier", default=None, help="path to patch-openclaw-config.py")
    parser.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    args = parser.parse_args(argv[1:])

    applier = Path(args.applier) if args.applier else DEFAULT_APPLIER
    try:
        _, lines = sync(Path(args.template), Path(args.config), applier, args.dry_run)
    except Exception as err:  # noqa: BLE001 - never block the boot
        print(f"reply gates NOT synced: {err}", file=sys.stderr)
        return 0
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
