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
DEFAULT_PROMPT_PATH = Path.home() / ".openclaw" / "workspace" / "docs" / "public-whatsapp-system-prompt.txt"
DEFAULT_STORE_PATH = Path.home() / ".openclaw" / "workspace" / "invites" / "invites.json"
PUBLIC_GATE_PLUGIN = "public-service-gate"


class ConfigError(Exception):
    pass


def utc_now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    path.parent.mkdir(parents=True, exist_ok=True)
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


def load_member_store(path):
    try:
        data = load_json(path)
    except ConfigError as exc:
        if "config file not found" not in str(exc):
            raise
        return {"invites": {}, "members": {}}
    if not isinstance(data, dict) or not isinstance(data.get("invites", {}), dict):
        raise ConfigError(f"unexpected invite store shape in {path}")
    if not isinstance(data.get("members", {}), dict):
        raise ConfigError(f"unexpected members shape in {path}")
    data.setdefault("invites", {})
    data.setdefault("members", {})
    return data


def unlocked_member_phones(store):
    phones = []
    for phone, record in store.get("members", {}).items():
        if not isinstance(record, dict) or record.get("status") != "unlocked":
            continue
        phones.append(normalize_phone(record.get("phone") or phone))
    return dedupe(phones)


def upsert_unlocked_member(store, phone, *, source):
    members = store.setdefault("members", {})
    now = utc_now_iso()
    record = members.get(phone)
    changed = False
    if not isinstance(record, dict):
        record = {}
        members[phone] = record
        changed = True
    for key, value in {"phone": phone, "status": "unlocked"}.items():
        if record.get(key) != value:
            record[key] = value
            changed = True
    if "activatedAt" not in record:
        record["activatedAt"] = now
        changed = True
    if source and record.get("source") != source:
        record["source"] = source
        changed = True
    if changed:
        record["updatedAt"] = now
    return changed


def remove_member(store, phone):
    return store.setdefault("members", {}).pop(phone, None) is not None


def open_dms_enabled(plugin_config):
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


def sync_config(cfg, prompt, store):
    plugin_config = get_plugin_config(cfg)
    whatsapp = get_whatsapp_config(cfg)
    config_public_users = normalize_phone_list(plugin_config.get("publicUsers", []), field_name="publicUsers")
    store_changed = False

    if open_dms_enabled(plugin_config):
        for phone in config_public_users:
            store_changed = upsert_unlocked_member(store, phone, source="migrated-openclaw-json") or store_changed
        plugin_config.pop("publicUsers", None)
        public_users = unlocked_member_phones(store)
    else:
        public_users = config_public_users

    admin_users = seed_admin_users(plugin_config, whatsapp, public_users)
    plugin_config["whatsappAdminUsers"] = admin_users

    if open_dms_enabled(plugin_config):
        whatsapp["dmPolicy"] = "open"
        whatsapp["allowFrom"] = ["*"]
    else:
        plugin_config["publicUsers"] = public_users
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
        "storeChanged": store_changed,
    }


def add_user(cfg, store, phone):
    plugin_config = get_plugin_config(cfg)
    if open_dms_enabled(plugin_config):
        return upsert_unlocked_member(store, phone, source="manual")
    public_users = normalize_phone_list(plugin_config.get("publicUsers", []), field_name="publicUsers")
    if phone in public_users:
        return False
    public_users.append(phone)
    plugin_config["publicUsers"] = public_users
    return True


def remove_user(cfg, store, phone):
    plugin_config = get_plugin_config(cfg)
    if open_dms_enabled(plugin_config):
        return remove_member(store, phone)
    public_users = normalize_phone_list(plugin_config.get("publicUsers", []), field_name="publicUsers")
    next_users = [value for value in public_users if value != phone]
    plugin_config["publicUsers"] = next_users
    return len(next_users) != len(public_users)


def check_config(cfg, prompt, store):
    plugin_config = get_plugin_config(cfg)
    whatsapp = get_whatsapp_config(cfg)
    config_public_users = normalize_phone_list(plugin_config.get("publicUsers", []), field_name="publicUsers")
    public_users = unlocked_member_phones(store) if open_dms_enabled(plugin_config) else config_public_users
    admin_users = normalize_phone_list(plugin_config.get("whatsappAdminUsers", []), field_name="whatsappAdminUsers")
    direct = whatsapp.get("direct") or {}
    errors = []
    if open_dms_enabled(plugin_config):
        if config_public_users:
            errors.append("plugins.entries.public-service-gate.config.publicUsers must be migrated to the invite member store")
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
    print(f"memberStoreChanged: {'yes' if details.get('storeChanged') else 'no'}")
    print("Reload or restart OpenClaw if the running gateway does not pick up config changes automatically.")


def main(argv):
    parser = argparse.ArgumentParser(description="Manage public WhatsApp users.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to openclaw.json")
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT_PATH), help="Path to the shared public WhatsApp prompt")
    parser.add_argument("--store", default=str(DEFAULT_STORE_PATH), help="Path to the WhatsApp invite/member store")
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
    store_path = Path(args.store).expanduser()
    cfg = load_json(config_path)
    prompt = load_prompt(prompt_path)
    store = load_member_store(store_path)
    config_changed = False
    store_changed = False
    phone = None

    if args.command == "add":
        phone = normalize_phone(args.phone)
        store_changed = add_user(cfg, store, phone)
        before = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        details = sync_config(cfg, prompt, store)
        config_changed = json.dumps(cfg, sort_keys=True, ensure_ascii=False) != before
        store_changed = details["storeChanged"] or store_changed
    elif args.command == "remove":
        phone = normalize_phone(args.phone)
        store_changed = remove_user(cfg, store, phone)
        before = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        details = sync_config(cfg, prompt, store)
        config_changed = json.dumps(cfg, sort_keys=True, ensure_ascii=False) != before
        store_changed = details["storeChanged"] or store_changed
    elif args.command == "sync":
        before = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        details = sync_config(cfg, prompt, store)
        config_changed = json.dumps(cfg, sort_keys=True, ensure_ascii=False) != before
        store_changed = details["storeChanged"]
    elif args.command == "list":
        details = sync_config(cfg, prompt, store)
        print("Public users:")
        for value in details["publicUsers"]:
            print(f"- {value}")
        print("WhatsApp admin users:")
        for value in details["whatsappAdminUsers"]:
            print(f"- {value}")
        return 0
    elif args.command == "check":
        errors = check_config(cfg, prompt, store)
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print("OK: WhatsApp public-user config is consistent.")
        return 0
    else:
        raise ConfigError(f"unknown command: {args.command}")

    if config_changed:
        write_json_atomic(config_path, cfg, backup=not args.no_backup)
    if store_changed:
        write_json_atomic(store_path, store, backup=False)
    details["storeChanged"] = store_changed
    print_summary(args.command, phone, details, config_changed or store_changed)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
