#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path


DEFAULT_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
DEFAULT_PROMPT_PATH = Path.home() / ".openclaw" / "workspace" / "public-whatsapp-system-prompt.txt"
PUBLIC_GATE_PLUGIN = "public-service-gate"


class ConfigError(Exception):
    pass


def normalize_phone(value):
    raw = str(value or "").strip()
    if not raw:
        raise ConfigError("phone number is empty")
    if raw == "*" or "@" in raw:
        raise ConfigError(f"not a direct phone number: {raw}")
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 8 or len(digits) > 15:
        raise ConfigError(f"invalid phone number length after normalization: {raw}")
    return f"+{digits}"


def normalize_phone_list(values, *, field_name):
    if values is None:
        return []
    if not isinstance(values, list):
        raise ConfigError(f"{field_name} must be a list")
    result = []
    seen = set()
    for value in values:
        phone = normalize_phone(value)
        if phone not in seen:
            result.append(phone)
            seen.add(phone)
    return result


def dedupe(values):
    result = []
    seen = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def prompt_key(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def same_prompt(left, right):
    return prompt_key(left) == prompt_key(right)


def load_json(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc


def load_prompt(path):
    try:
        prompt = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"prompt file not found: {path}") from exc
    if not prompt:
        raise ConfigError(f"prompt file is empty: {path}")
    return prompt


def write_json_atomic(path, data, *, backup):
    if backup and path.exists():
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def open_dms_enabled(plugin_config):
    # Same flag the public-service-gate plugin reads to switch on peer invites.
    return bool(plugin_config.get("invitesEnabled"))


def get_plugin_config(cfg):
    plugins = cfg.setdefault("plugins", {})
    entries = plugins.setdefault("entries", {})
    plugin = entries.setdefault(PUBLIC_GATE_PLUGIN, {})
    plugin.setdefault("enabled", True)
    config = plugin.setdefault("config", {})
    config.setdefault("enabled", True)
    return config


def get_whatsapp_config(cfg):
    channels = cfg.setdefault("channels", {})
    whatsapp = channels.setdefault("whatsapp", {})
    if not isinstance(whatsapp, dict):
        raise ConfigError("channels.whatsapp must be an object")
    return whatsapp


def seed_admin_users(plugin_config, whatsapp, public_users):
    existing = plugin_config.get("whatsappAdminUsers")
    if existing is not None:
        return normalize_phone_list(existing, field_name="whatsappAdminUsers")
    allow_from = []
    for value in whatsapp.get("allowFrom", []) or []:
        if value == "*":
            continue
        allow_from.append(value)
    normalized_allow = normalize_phone_list(allow_from, field_name="channels.whatsapp.allowFrom")
    public = set(public_users)
    return [phone for phone in normalized_allow if phone not in public]


def sync_config(cfg, prompt):
    plugin_config = get_plugin_config(cfg)
    whatsapp = get_whatsapp_config(cfg)

    public_users = normalize_phone_list(plugin_config.get("publicUsers", []), field_name="publicUsers")
    admin_users = seed_admin_users(plugin_config, whatsapp, public_users)

    plugin_config["publicUsers"] = public_users
    plugin_config["whatsappAdminUsers"] = admin_users

    # When peer invites / open DMs are enabled, the WhatsApp channel must accept any
    # sender ("*"); membership is then enforced by the public-service-gate plugin, not
    # the channel allowlist. publicUsers/whatsappAdminUsers stay canonical for the gate.
    if open_dms_enabled(plugin_config):
        whatsapp["dmPolicy"] = "open"
        whatsapp["allowFrom"] = ["*"]
    else:
        # Classic allowlist mode (also reverts an "open" policy left from a prior run).
        whatsapp["dmPolicy"] = "allowlist" if whatsapp.get("dmPolicy") in (None, "open") else whatsapp["dmPolicy"]
        whatsapp["allowFrom"] = dedupe(admin_users + public_users)

    direct = whatsapp.get("direct")
    if direct is None:
        direct = {}
    if not isinstance(direct, dict):
        raise ConfigError("channels.whatsapp.direct must be an object when present")

    next_direct = {"*": {"systemPrompt": prompt}}
    for peer, value in direct.items():
        if peer == "*":
            continue
        if not isinstance(value, dict):
            next_direct[peer] = value
            continue
        peer_prompt = value.get("systemPrompt")
        rest = {key: item for key, item in value.items() if key != "systemPrompt"}
        if peer_prompt is not None and str(peer_prompt).strip() and not same_prompt(peer_prompt, prompt):
            rest["systemPrompt"] = str(peer_prompt).strip()
        if rest:
            next_direct[peer] = rest
    whatsapp["direct"] = next_direct

    return {
        "publicUsers": public_users,
        "whatsappAdminUsers": admin_users,
        "allowFrom": whatsapp["allowFrom"],
    }


def add_user(cfg, phone):
    plugin_config = get_plugin_config(cfg)
    public_users = normalize_phone_list(plugin_config.get("publicUsers", []), field_name="publicUsers")
    if phone in public_users:
        changed = False
    else:
        public_users.append(phone)
        changed = True
    plugin_config["publicUsers"] = public_users
    return changed


def remove_user(cfg, phone):
    plugin_config = get_plugin_config(cfg)
    public_users = normalize_phone_list(plugin_config.get("publicUsers", []), field_name="publicUsers")
    next_users = [value for value in public_users if value != phone]
    plugin_config["publicUsers"] = next_users
    return len(next_users) != len(public_users)


def check_config(cfg, prompt):
    plugin_config = get_plugin_config(cfg)
    whatsapp = get_whatsapp_config(cfg)
    public_users = normalize_phone_list(plugin_config.get("publicUsers", []), field_name="publicUsers")
    admin_users = normalize_phone_list(plugin_config.get("whatsappAdminUsers", []), field_name="whatsappAdminUsers")
    direct = whatsapp.get("direct") or {}
    errors = []
    if open_dms_enabled(plugin_config):
        if whatsapp.get("dmPolicy") != "open":
            errors.append('channels.whatsapp.dmPolicy must be "open" when invitesEnabled is set')
        if (whatsapp.get("allowFrom") or []) != ["*"]:
            errors.append('channels.whatsapp.allowFrom must be ["*"] when invitesEnabled is set')
    else:
        expected_allow = dedupe(admin_users + public_users)
        actual_allow = normalize_phone_list(
            [value for value in whatsapp.get("allowFrom", []) or [] if value != "*"],
            field_name="channels.whatsapp.allowFrom",
        )
        if actual_allow != expected_allow:
            errors.append("channels.whatsapp.allowFrom is not the derived union of whatsappAdminUsers + publicUsers")
    wildcard_prompt = ((direct.get("*") or {}).get("systemPrompt") or "").strip() if isinstance(direct, dict) else ""
    if wildcard_prompt != prompt:
        errors.append('channels.whatsapp.direct["*"].systemPrompt does not match the shared prompt file')
    if isinstance(direct, dict):
        for peer, value in direct.items():
            if peer == "*" or not isinstance(value, dict):
                continue
            peer_prompt = (value.get("systemPrompt") or "").strip()
            if same_prompt(peer_prompt, prompt):
                errors.append(f"repeated shared prompt still exists at channels.whatsapp.direct.{peer}.systemPrompt")
    else:
        errors.append("channels.whatsapp.direct must be an object")
    return errors


def print_summary(action, phone, details, changed):
    phone_part = f" {phone}" if phone else ""
    status = "changed" if changed else "unchanged"
    print(f"{action}{phone_part}: {status}")
    print(f"publicUsers: {len(details['publicUsers'])}")
    print(f"whatsappAdminUsers: {len(details['whatsappAdminUsers'])}")
    print(f"allowFrom: {len(details['allowFrom'])}")
    print("Reload or restart OpenClaw if the running gateway does not pick up config changes automatically.")


def main(argv):
    parser = argparse.ArgumentParser(description="Manage public WhatsApp users in openclaw.json.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to openclaw.json")
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT_PATH), help="Path to the shared public WhatsApp prompt")
    parser.add_argument("--no-backup", action="store_true", help="Do not create a timestamped config backup before writing")
    sub = parser.add_subparsers(dest="command", required=True)

    add_parser = sub.add_parser("add", help="Add a public WhatsApp user")
    add_parser.add_argument("phone")
    remove_parser = sub.add_parser("remove", help="Remove a public WhatsApp user")
    remove_parser.add_argument("phone")
    sub.add_parser("list", help="List configured public and admin WhatsApp users")
    sub.add_parser("sync", help="Rebuild derived WhatsApp config from canonical lists")
    sub.add_parser("check", help="Validate config consistency without writing")

    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser()
    prompt_path = Path(args.prompt).expanduser()
    cfg = load_json(config_path)
    prompt = load_prompt(prompt_path)
    changed = False
    phone = None

    if args.command == "add":
        phone = normalize_phone(args.phone)
        changed = add_user(cfg, phone)
        details = sync_config(cfg, prompt)
    elif args.command == "remove":
        phone = normalize_phone(args.phone)
        changed = remove_user(cfg, phone)
        details = sync_config(cfg, prompt)
    elif args.command == "sync":
        before = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        details = sync_config(cfg, prompt)
        changed = json.dumps(cfg, sort_keys=True, ensure_ascii=False) != before
    elif args.command == "list":
        details = sync_config(cfg, prompt)
        print("Public users:")
        for value in details["publicUsers"]:
            print(f"- {value}")
        print("WhatsApp admin users:")
        for value in details["whatsappAdminUsers"]:
            print(f"- {value}")
        return 0
    elif args.command == "check":
        errors = check_config(cfg, prompt)
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print("OK: WhatsApp public-user config is consistent.")
        return 0
    else:
        raise ConfigError(f"unknown command: {args.command}")

    if changed:
        write_json_atomic(config_path, cfg, backup=not args.no_backup)
    print_summary(args.command, phone, details, changed)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
